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
  * the catalogue's OVER-THE-BOX scenarios (``overrun:`` checks) run each
    acquisition route past its source's coverage: the run must complete,
    warn, read 0 m sea level in the Site Analysis view and keep VOID in Map
    Converter output — the 2026-08-31 no-data contract, per route;
  * the FOLDER input (``folder:``), a real conversion per catalogue
    resolution with byte-exact tile sizes (``sweep:``), the other models and
    both backends, a multi-tile range, the cache drills and the scripted P2P
    (``matrix:``), and the height/batch gates (tier A ``gate:``/``batch:``)
    — the checklist's Phases 3, 7, 8 and 9, automated. A ``--rows`` run
    skips the case tiers (they are not row-addressable) and says so;
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
  SKIP    the check does not apply, or its prerequisite is genuinely absent —
          and it FAILS the run: a prerequisite someone forgot is a hole
  NOTRUN  the manifest declares this check and it did not execute — a hole in
          the run, counted, reported, and it FAILS the run

Results go to ``data/torture/results.json`` and a Markdown summary, which names
the tiers and filters the run used — a filtered re-run overwrites the same two
files, and its report says PARTIAL rather than PASSED so nobody reads a slice
as the whole set. Exit code: 0 = everything the manifest declares ran and
passed; 1 = something FAILED, XPASSed, SKIPped or did not run — a check that
does not run is a failed run, and no flag changes that (``--allow-incomplete``
is accepted for old command lines and ignored); 2 = the invocation itself was
unusable (no manifest, bad --tier, a --rows filter that selects nothing, or a
run that made zero checks). A suite that quietly drops half its checks and
still exits 0 is the failure mode this replaces.

Run procedure (Windows)::

  1. Plugin code changed?     python deploy.py        (installs, restarts QGIS)
  2. Catalogue changed?       python tools\\make_torture_project.py
  3. Tiers a/b: QGIS console or standalone — they test whatever waveshed is
     loaded, and say which in the plugin.import line.
     Tier C: STANDALONE ONLY —
       run_torture.bat [--rows ... --tier ...]     (repo root; finds
       python-qgis.bat via TORTURE_PYQGIS, torture.local.ini's qgis_exe,
       or the default OSGeo4W / Program Files locations)
     It drives the GUI workers synchronously (a live QGIS would freeze for
     the whole tier), and standalone the import is guaranteed to be THIS
     repo's plugin — the plugin.pairing check refuses anything else.
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
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

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

    class Backlogged(http.server.ThreadingHTTPServer):
        # The Rust downloader opens up to 256 connections at once
        # (waveshed/download_connections). TCPServer's default listen
        # backlog of 5 refuses the burst, and every refused connection
        # is a REAL failure to the no-data contract — it counted as
        # connect= errors that majority-aborted over-the-box runs.
        request_queue_size = 512

    try:
        server = Backlogged(
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


def borrowed_qgis(app) -> bool:
    """True when a QgsApplication exists that THIS RUN did not create.

    That is the QGIS-console case tier C must refuse. ``running_inside_qgis``
    alone cannot decide it: after tiers a/b the runner's OWN application
    exists, and 2026-08-26 the guard read that as "inside a live QGIS" and
    refused a perfectly standalone --tier abc run.
    """
    return app is None and running_inside_qgis()


def _default_profile_folder() -> str:
    """The desktop QGIS profile folder, or "" when none exists.

    A standalone QgsApplication started WITHOUT a profile folder reads an
    empty settings store — so on 2026-08-26 a fully licensed machine
    reported "API key required" on every engine run: the Waveshed key (and
    binary dir) live in QgsSettings inside the DESKTOP profile. Honouring
    ``profiles.ini``'s defaultProfile keeps named-profile setups working.
    """
    import platform as _platform
    system = _platform.system()
    if system == "Windows":
        root = Path(os.environ.get("APPDATA", "")) / "QGIS" / "QGIS3"
    elif system == "Darwin":
        root = Path.home() / "Library" / "Application Support" / "QGIS" / "QGIS3"
    else:
        root = Path.home() / ".local" / "share" / "QGIS" / "QGIS3"
    profiles = root / "profiles"
    name = "default"
    ini = profiles / "profiles.ini"
    if ini.is_file():
        try:
            import configparser
            parser = configparser.ConfigParser()
            parser.read(str(ini), encoding="utf-8")
            name = parser.get("core", "defaultProfile",
                              fallback="default") or "default"
        except Exception:  # noqa: BLE001 — an unreadable ini means "default"
            name = "default"
    folder = profiles / name
    return str(folder) if folder.is_dir() else ""


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
    # The desktop app sets the Qt org/app identity and the settings path
    # BEFORE anything reads a setting; QgsApplication alone does neither, so
    # a standalone QgsSettings resolved to ".../Unknown Organization.ini"
    # and every stored value — the Waveshed API key above all — read as
    # empty (2026-08-26, reproduced; the constructor's profileFolder alone
    # does not fix it). These four calls make QgsSettings land on the
    # desktop profile's <profile>/QGIS/QGIS3.ini, verified by fileName().
    from qgis.PyQt.QtCore import QCoreApplication, QSettings
    QCoreApplication.setOrganizationName("QGIS")
    QCoreApplication.setOrganizationDomain("qgis.org")
    QCoreApplication.setApplicationName("QGIS3")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    profile = _default_profile_folder()
    if profile:
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, profile)
    # GUI enabled: tier C drives the tabs' own main-thread resolve, whose
    # progress dialog is a real widget. Offscreen, it draws to nowhere —
    # but it must be constructible, exactly as in the GUI.
    app = QgsApplication([], True, profile)
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
    if row.get("expect_tile_px") and "type=xyz" in (row.get("source") or ""):
        results.plan(name, "xyz.tile_px")
    if row.get("expect_format") and "type=xyz" in (row.get("source") or ""):
        results.plan(name, "xyz.format")
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
        loaded, version = _plugin_identity()
        results.add("-", "plugin.import", "import the waveshed plugin", PASS,
                    f"waveshed {version} at {loaded}")
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
    _tier_a_gates(manifest, results, out_dir)

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
    """Every tier-A check for one catalogue row.

    Both bail-outs below go through :func:`_fail_rest`: the row declared
    ``classify``/``encoding``/``zmax``/``render``/… before anything ran, and
    walking away from them left the report saying "declared, never executed"
    where it could say which check was owed and what stopped it.
    """
    layer = _resolve_layer(row, by_name, results)
    if layer is None:
        _fail_rest(row["row"], row, results,
                   "the catalogue's layer is not in the project")
        return
    if not _check_layer_loads(row, layer, results, out_dir):
        _fail_rest(row["row"], row, results,
                   "the layer did not load, so nothing below it could be asked")
        return
    if isinstance(layer, QgsRasterLayer):
        _check_raster_verdicts(row, layer, results, adapter,
                               classify_raster_layer, dem_layer_warning)
        _check_tile_px(row, layer.source() or "", results, manifest, adapter)
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
                         "zmax.service_limit", "xyz.tile_px"):
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
    ``zmax=18``: without a ``max_zoom`` entry the plugin cannot clamp it,
    every request past the real limit answers 404 — which the no-data
    contract turns into a warned, sea-filled result. Better than the silent
    flat sea it used to be, but still sea where the user wanted terrain the
    service DOES publish one zoom down; the clamp is what gets them that
    terrain.

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
                  f"z{expected} answers 404 and the run degrades to warned sea level "
                  f"instead of the terrain the service publishes at z{expected}. "
                  f"Add \"max_zoom\": {expected}.")
    results.add(row["row"], "zmax.service_limit",
                f"the plugin knows this service stops at z{expected}",
                PASS if ok else FAIL, detail,
                known_fail_for(row, "zmax.service_limit"))


#: Magic-byte sniffers for ``expect_format`` — the format the service REALLY
#: serves, decided from the bytes: row 1.4's service answers WebP for a
#: ``.png`` request, so neither Content-Type nor the URL's extension counts.
_FORMAT_MAGIC = {
    "png": lambda b: b[:8] == b"\x89PNG\r\n\x1a\n",
    "webp": lambda b: b[:4] == b"RIFF" and b[8:12] == b"WEBP",
    "jpeg": lambda b: b[:3] == b"\xff\xd8\xff",
}


def sniff_image_format(data: bytes) -> str:
    """"png" | "webp" | "jpeg" | a hex preview for anything else."""
    for name, match in _FORMAT_MAGIC.items():
        if len(data) >= 12 and match(data):
            return name
    return f"unknown ({data[:8]!r})"


def _check_tile_px(row: Dict[str, Any], source: str, results: Results,
                   manifest, adapter) -> None:
    """The tile edge AND byte format this service REALLY serves, on one tile.

    ``expect_tile_px`` was written into the manifest and read by nobody —
    the classic way a catalogue field rots (``expect_format`` rotted the
    same way until this check learned to read it). A 512 px (@2x) service
    assembled with 256 px maths folds every tile into terrain that is
    hundreds of metres wrong (the 1.3a defect), so the suite fetches ONE
    tile through the layer's own URL template and measures the actual
    image; the same bytes answer ``expect_format`` by magic numbers. The
    zoom is the one the PLUGIN would fetch at — its own ``resolve_zmax``
    clamp — never the URI's raw ``zmax``: row 1.2 carries QGIS's default
    zmax=18 on purpose, and terrarium answers 404 above z15.
    """
    check_id = "xyz.tile_px"
    expected = int(row.get("expect_tile_px") or 0)
    expected_format = (row.get("expect_format") or "").strip().lower()
    if "type=xyz" not in (source or "") or not (expected or expected_format):
        results.unplan(row["row"], check_id)
        results.unplan(row["row"], "xyz.format")
        return
    if not expected:
        results.unplan(row["row"], check_id)
    if not expected_format:
        results.unplan(row["row"], "xyz.format")
    label = f"the service serves {expected} px tiles"
    probe = (manifest or {}).get("elevation_probe") or {}
    lat = float(probe.get("lat", 46.945))
    lon = float(probe.get("lon", 7.41))
    z = int_param(uri_params(source), "zmax", 15)
    if adapter is not None:
        try:
            z = int(adapter.resolve_zmax(source)[0])
        except Exception:
            pass  # the URI's own zmax stays the fallback
    n = 1 << z
    x = min(n - 1, max(0, int((lon + 180.0) / 360.0 * n)))
    y = min(n - 1, max(0, int(
        (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)))
    url = (xyz_template(source).replace("{z}", str(z))
           .replace("{x}", str(x)).replace("{y}", str(y)))
    try:
        import urllib.request
        req = urllib.request.Request(
            url, headers={"User-Agent": "waveshed-torture/1"})
        with urllib.request.urlopen(req, timeout=20) as reply:
            data = reply.read()
    except Exception as exc:
        detail = (f"could not fetch {url}: {type(exc).__name__}: "
                  f"{_shorten(str(exc), 90)}")
        if expected:
            results.add(row["row"], check_id, label, FAIL, detail,
                        known_fail_for(row, check_id))
        if expected_format:
            results.add(row["row"], "xyz.format",
                        f"the service serves {expected_format} bytes", FAIL,
                        detail, known_fail_for(row, "xyz.format"))
        return

    if expected_format:
        served = sniff_image_format(data)
        ok = served == expected_format
        results.add(row["row"], "xyz.format",
                    f"the service serves {expected_format} bytes",
                    PASS if ok else FAIL,
                    f"z{z} tile is {served}"
                    + ("" if ok else f" — the catalogue says {expected_format}; "
                                     f"a changed byte format changes what the "
                                     f"toolkit decoder can read"),
                    known_fail_for(row, "xyz.format"))
    if not expected:
        return
    from qgis.PyQt.QtGui import QImage
    img = QImage.fromData(data)
    if img.isNull():
        results.add(row["row"], check_id, label, FAIL,
                    f"{url} answered {len(data)} bytes that decode as no "
                    f"image (first bytes {data[:8]!r})",
                    known_fail_for(row, check_id))
        return
    w, h = img.width(), img.height()
    ok = w == expected and h == expected
    results.add(row["row"], check_id, label, PASS if ok else FAIL,
                f"z{z} tile measures {w}x{h} px"
                + ("" if ok else f" — the catalogue says {expected}, and the "
                                 f"resolution maths runs on the real size"),
                known_fail_for(row, check_id))


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


def _tier_a_gates(manifest: Dict[str, Any], results: Results,
                  out_dir: Path) -> None:
    """The height gates and batch-CSV contracts — checklist Phase 8's
    parser half, engine-free and therefore tier A's.

    Drives the shipped functions (``antenna_height_error``, the P2P tab's
    ``_parse_batch_csv``) with the catalogue's own cases: 0.5 m AGL must be
    rejected on either antenna, -430 m AMSL accepted, -501 m rejected, and
    every ``batch_cases`` file must be byte-for-byte the one the catalogue
    describes and parse (or refuse, naming its line and cause) as declared.
    """
    gates = ("gate:agl-tx-floor", "gate:agl-rx-floor",
             "gate:amsl-negative-ok", "gate:amsl-floor")
    for check_id in gates:
        results.plan("8", check_id)
    batch_cases = manifest.get("batch_cases") or []
    for case in batch_cases:
        results.plan("8", f"batch:{case['file']}")

    try:
        from waveshed.core.job_builder import CoverageParams, antenna_height_error
        from waveshed.gui.p2p_tab import _parse_batch_csv
    except ImportError as exc:
        results.add("8", "gate:agl-tx-floor", "the height gates are importable",
                    FAIL, f"{exc} — every Phase 8 gate check is lost")
        _fail_rest("8", None, results,
                   f"the height gates could not be imported ({exc})")
        return

    def params(tx_height=30.0, tx_mode="AGL", rx_height=2.0, rx_mode="AGL"):
        return CoverageParams(
            tx_lat=46.9481, tx_lon=7.4474, tx_height=tx_height, tx_mode=tx_mode,
            freq_mhz=900.0, erp_watts=10.0, rx_height=rx_height, rx_mode=rx_mode,
            model="ITM", resolution_m=30, max_range_km=3, backend="CPU",
            output_name="gate", max_ram_gb=8, max_vram_gb=4)

    for check_id, kwargs, want_reject, label in (
            ("gate:agl-tx-floor", {"tx_height": 0.5}, True,
             "0.5 m AGL on the transmitter is rejected"),
            ("gate:agl-rx-floor", {"rx_height": 0.5}, True,
             "0.5 m AGL on the receiver is rejected"),
            ("gate:amsl-negative-ok", {"tx_height": -430.0, "tx_mode": "AMSL"},
             False, "-430 m AMSL is accepted (the Dead Sea site)"),
            ("gate:amsl-floor", {"tx_height": -501.0, "tx_mode": "AMSL"}, True,
             "-501 m AMSL is rejected (the floor is -500)")):
        try:
            said = antenna_height_error(params(**kwargs))
        except Exception as exc:  # noqa: BLE001 — a crash is its own failure
            results.add("8", check_id, label, FAIL,
                        f"antenna_height_error raised {type(exc).__name__}: {exc}")
            continue
        rejected = bool(said)
        ok = rejected == want_reject
        results.add("8", check_id, label, PASS if ok else FAIL,
                    _shorten(" ".join((said or "accepted").split()), 100)
                    if ok else
                    (f"accepted — the gate is the only thing between a 0.5 m "
                     f"antenna and the engine" if want_reject else
                     f"rejected a legal value: {_shorten(said or '', 90)}"))

    for case in batch_cases:
        check_id = f"batch:{case['file']}"
        path = out_dir / "batch" / case["file"]
        label = (f"{case['file']} is {'accepted' if case['expect'] == 'accept' else 'rejected at its line'}")
        if not path.is_file():
            results.add("8", check_id, label, FAIL,
                        "the fixture is missing — run --stages vectors")
            continue
        on_disk = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
        declared = str(case.get("content") or "")
        if declared and on_disk != declared:
            results.add("8", check_id, label, FAIL,
                        "the CSV on disk is not the one the catalogue "
                        "describes — regenerate (--stages vectors)")
            continue
        try:
            entries = _parse_batch_csv(str(path))
        except ValueError as exc:
            said = str(exc)
            if case["expect"] == "accept":
                results.add("8", check_id, label, FAIL,
                            f"the parser refused an accept case: {_shorten(said, 100)}")
                continue
            wanted_line = f"Line {case.get('line')}"
            must = str(case.get("must_name") or "")
            ok = wanted_line in said and (not must or must in said)
            results.add("8", check_id, label, PASS if ok else FAIL,
                        _shorten(said, 100) if ok else
                        f"refused, but not naming {wanted_line}/{must!r}: "
                        f"{_shorten(said, 90)}")
            continue
        if case["expect"] != "accept":
            results.add("8", check_id, label, FAIL,
                        f"the parser accepted {len(entries)} row(s) from a file "
                        f"the catalogue says must be rejected at line "
                        f"{case.get('line')}")
            continue
        wanted_rows = int(case.get("expect_rows") or 0)
        ok = not wanted_rows or len(entries) == wanted_rows
        results.add("8", check_id, label, PASS if ok else FAIL,
                    f"{len(entries)} row(s) parsed"
                    + ("" if ok else f", catalogue says {wanted_rows}"))


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
# The per-row cache environment (tier C proper starts further down)
# ---------------------------------------------------------------------------
#
# The engine binaries are the real ones, discovered by the plugin's own
# binary_manager. There is no stub anywhere in this file, and there must
# never be: a test double for the component under test proves only that the
# double behaves like the double.


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

    def __init__(self, scratch: Path, keep: bool = False) -> None:
        self.scratch = scratch
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


def _plugin_modules():
    """The plugin entry points the pipeline drives. Imported once, lazily."""
    from waveshed.core import abt as abt_mod
    from waveshed.core import buildings_source
    from waveshed.core import result_loader
    from waveshed.core import terrain_adapter as adapter
    from waveshed.core.job_builder import (CoverageParams, P2PParams,
                                           antenna_height_error,
                                           build_coverage_job, build_p2p_job,
                                           model_warnings)
    from waveshed.core.layer_utils import dem_layer_warning
    from waveshed.gui import map_converter_tab as mc
    from waveshed.gui import p2p_tab as p2p
    from waveshed.gui import site_analysis_tab as sat
    return {"abt": abt_mod, "adapter": adapter, "mc": mc, "sat": sat,
            "p2p": p2p, "result_loader": result_loader,
            "buildings_source": buildings_source,
            "CoverageParams": CoverageParams, "P2PParams": P2PParams,
            "build_coverage_job": build_coverage_job,
            "build_p2p_job": build_p2p_job,
            "antenna_height_error": antenna_height_error,
            "model_warnings": model_warnings,
            "dem_layer_warning": dem_layer_warning}


#: What ``aether_converter`` writes, in i16 counts, for a pixel no source
#: covered. Stated here as the engine's own number, deliberately NOT imported
#: from the plugin — ``_check_void_sentinel`` compares the plugin's validity
#: floor against it, and a floor derived from the same constant it is being
#: checked against would pass by construction. The floor used to be -5000
#: *metres* (-10000 counts), one count BELOW this, so every hole read as valid
#: ground 5 km down.
CONVERTER_VOID_COUNTS = -9999


def _real_mask(grid, plugin):
    """Pixels that are terrain: above the plugin's floor AND not the sentinel.

    The second half is belt and braces: the plugin's floor now rejects the
    sentinel on its own (that is what ``_check_void_sentinel`` asserts), but a
    comparison here that treated a hole as ground would be measuring the very
    mistake this suite exists to catch, so it is stated rather than assumed.
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


# ---------------------------------------------------------------------------
# Tier C — the pipeline, end to end, through the SHIPPED workers
# ---------------------------------------------------------------------------
#
# Nothing here re-implements the plugin, and nothing here drives a layer of
# the plugin below the one the user's click drives. Every run goes through
# the object the GUI itself constructs:
#
#   Map Converter    map_converter_tab.resolve_sources_with_progress (main
#                    thread, imagery guard, per-tile renders) and then
#                    map_converter_tab._MapConverterWorker.run — plan
#                    cross-check, XYZ pool download, _build_tile_jobs, ONE
#                    array job file through run_converter_streaming.
#   Site Analysis    site_analysis_tab._SiteAnalysisWorker.run —
#                    prepare_terrain (wedge, buildings, void-fill),
#                    build_coverage_job + write_job_file, licensed
#                    aether_core through run_converter_streaming,
#                    aether_export, and then result_loader on what came out.
#   P2P              p2p_tab._P2PWorker.run, fed by the tab's own
#                    _write_temp_batch_csv.
#   Processing       processing.run("waveshed:coverage", …) — the scripted
#                    surface, which shows the user no prompt at all.
#
# The runner's own code is limited to three jobs: build the inputs a user
# would type, collect the workers' signals, and assert what came out. If a
# stage looks missing here, it is because the worker owns it.

def _layer_entry(row: Dict[str, Any], layer, plugin, resolutions: Sequence[int],
                 extent: Optional[Dict[str, float]] = None):
    """One Map Converter stack entry for this row — the user's Add Layer."""
    mc = plugin["mc"]
    kind = "buildings" if row.get("kind") == "vector" else "raster"
    # native_res_m through the tab's OWN detection: it decides the export
    # resolution of a rendered service (min(native, finest output)), so
    # leaving it None made the runner render at a coarser grid than the GUI
    # — and manufactured the very cross-tab agreement tier C measures.
    native = None
    if kind == "raster" and layer is not None:
        try:
            native = mc._detect_resolution(layer)
        except Exception:  # noqa: BLE001 — the tab tolerates no-detection too
            native = None
    return mc._LayerEntry(
        layer_type=kind,
        source_path=layer.source() if layer is not None else row["source"],
        qgis_layer=layer,
        crs_authid=row.get("crs") or "EPSG:4326",
        native_res_m=native,
        target_resolutions=list(resolutions),
        extent=dict(extent) if extent else None,
    )


def _run_worker(worker, timeout: int) -> Dict[str, Any]:
    """Start a shipped worker QThread and wait for its verdict.

    Signals are collected over DIRECT connections (list appends are
    thread-safe; there is no event loop to queue through), the thread gets
    *timeout* seconds, and a worker that will not finish is cancelled
    through its own ``cancel()`` — the same kill path the GUI's Cancel
    button takes.
    """
    from qgis.PyQt.QtCore import Qt
    state: Dict[str, Any] = {"ok": None, "err": None, "log": []}

    def _took(value):
        state["ok"] = value

    def _failed(message):
        state["err"] = str(message)

    worker.finished_ok.connect(_took, Qt.ConnectionType.DirectConnection)
    worker.finished_err.connect(_failed, Qt.ConnectionType.DirectConnection)
    if hasattr(worker, "log_line"):
        worker.log_line.connect(state["log"].append, Qt.ConnectionType.DirectConnection)
    worker.status.connect(state["log"].append, Qt.ConnectionType.DirectConnection)
    # The plugin narrates the slow parts (writeRaster sizes and timings, pool
    # hits, the engine's own lines) through QgsMessageLog, not through worker
    # signals. Keep them: a timeout that only says "no answer" cannot be told
    # apart from a hang, a slow server or a slow engine — which is exactly
    # what row 4.3 (live WCS) reported on 2026-09-22.
    started = time.perf_counter()
    with _MessageLogTap() as tap:
        worker.start()
        finished = worker.wait(int(timeout) * 1000)
        if not finished:
            try:
                worker.cancel()
            except Exception:
                pass
            worker.wait(15000)
    state["elapsed"] = time.perf_counter() - started
    state["plugin_log"] = list(tap.lines)
    if not finished:
        state["err"] = (state["err"]
                        or f"no answer within {timeout}s — the worker was cancelled"
                        + _stall_report(state["log"], tap.lines))
    if state["ok"] is None and state["err"] is None:
        state["err"] = ("the worker finished without emitting a verdict "
                        "(cancelled mid-run?)")
    return state


def _stall_report(statuses: List[str], plugin_log: List[str]) -> str:
    """Where a worker stood when it was cancelled — the last thing it said.

    Pure, so the timeout wording can be unit-tested. The status line names
    the pipeline stage (terrain / job / engine / export); the plugin log line
    names the operation inside it (``writeRaster 1852x1852 …``). Both are
    quoted so the finding carries the evidence, not just the verdict.
    """
    def _last(lines: List[str]) -> str:
        for line in reversed(lines):
            text = " ".join(str(line).split())
            if text:
                return _shorten(text, 110)
        return ""
    status, logged = _last(statuses), _last(plugin_log)
    if not status and not logged:
        return "; it emitted no status and no log line at all"
    parts = []
    if status:
        parts.append(f"last status: {status!r}")
    if logged:
        parts.append(f"last log line: {logged!r}")
    return "; " + "; ".join(parts)


class _MessageLogTap:
    """Collects the plugin's own QgsMessageLog lines for the duration.

    The terrain adapter reports through ``QgsMessageLog`` ("Waveshed-Terrain"),
    not through worker signals — the no-data/sea-level warning, the pool-HIT
    line and the extent warning all live there. Asserting on them means
    listening where the plugin actually speaks; re-plumbing the plugin so the
    harness hears better would test a plugin nobody ships.
    """

    def __init__(self, tag_prefix: str = "Waveshed") -> None:
        self.lines: List[str] = []
        self._tag_prefix = tag_prefix

    def __enter__(self) -> "_MessageLogTap":
        def _collect(message, tag, _level):
            if str(tag or "").startswith(self._tag_prefix):
                self.lines.append(str(message))

        self._collect = _collect        # keep the reference for disconnect
        # Direct, for the same reason _run_worker collects directly: the
        # adapter logs from worker threads, and a queued delivery would need
        # an event loop this runner never spins — the tap read empty.
        try:
            from qgis.core import QgsApplication
            from qgis.PyQt.QtCore import Qt
            QgsApplication.messageLog().messageReceived.connect(
                _collect, Qt.ConnectionType.DirectConnection)
        except Exception:  # noqa: BLE001 — no message log (unit-test stubs)
            self._collect = None
        return self

    def __exit__(self, *exc) -> None:
        if self._collect is None:
            return
        from qgis.core import QgsApplication
        try:
            QgsApplication.messageLog().messageReceived.disconnect(self._collect)
        except Exception:  # noqa: BLE001 — a failed disconnect must not mask the run
            pass

    def text(self) -> str:
        return "\n".join(self.lines)


#: Complaints the RUNNER synthesises when a worker never answers. They must
#: never satisfy a must-fail/reject expectation — a hung worker is not a
#: refusal, and row 1.6's "any loud refusal" contract would otherwise be met
#: by the runner talking to itself.
_SYNTHESIZED_COMPLAINTS = ("no answer within", "finished without emitting a verdict")


def _is_synthesized(said: str) -> bool:
    low = said.lower()
    return any(marker in low for marker in _SYNTHESIZED_COMPLAINTS)


#: Worker/engine lines that carry PROGRESS and nothing else. They are dropped
#: from a complaint's log tail BEFORE it is truncated: on 2026-09-20 rows
#: 1.1/1.3b reported an anonymous "terrain out of this layer, through the real
#: Map Converter" failure because fifteen
#: "Downloading terrain (z12): 63/63 source tiles" lines were the entire tail,
#: and the engine's actual complaint had been pushed out of it.
_PROGRESS_LINE_RES = (
    re.compile(r"^Downloading terrain \(z\d+\):\s*\d+/\d+ source tiles$", re.I),
    re.compile(r"^Terrain download:\s*\d+/\d+ tiles ready$", re.I),
    re.compile(r"^Converting tile\s*\d+/\d+$", re.I),
    re.compile(r"^Resolved layer\s*\d+/\d+$", re.I),
    re.compile(r"^\[Download\]\s+\d+%\s+\(\d+/\d+\)"),
)

#: Words that keep a line even though it carries an N/M counter. The generic
#: "it is just a counter" rule below must never eat the one line that says why
#: the run failed, so anything that smells like a diagnosis is kept.
_COMPLAINT_MARKERS = (
    "error", "warn", "fail", "caused by", "panic", "refus", "abort", "denied",
    "cannot", "could not", "can't", "no-data", "nodata", "missing", "invalid",
    "unsupported", "unreadable", "timed out", "timeout", "exit code",
)


def _is_progress_line(line: str) -> bool:
    """Does this log line say only how far along the run is?

    Progress is the noise a complaint drowns in. Nothing that names a cause
    is dropped — a line matching one of the known progress shapes goes, and
    otherwise a bare ``N/M`` counter goes only when the line carries no
    diagnostic word at all.
    """
    text = " ".join(str(line).split())
    if not text:
        return True
    if any(rx.match(text) for rx in _PROGRESS_LINE_RES):
        return True
    if any(marker in text.lower() for marker in _COMPLAINT_MARKERS):
        return False
    return bool(re.search(r"\b\d+\s*/\s*\d+\b", text)) and len(text) <= 120


def _worker_complaint(state: Dict[str, Any]) -> str:
    """The engine's own words, with the worker's verdict LAST.

    A worker reports engine failure as "Converter failed (exit code N). See
    log." — the refusal the catalogue wants named (the file, the codec, the
    reason) is in the streamed log lines. Both go into the complaint, or a
    correct refusal reads as an anonymous one.

    Two rules keep the cause visible. The progress chatter is dropped BEFORE
    the last fifteen lines are taken, and the worker's ``err`` is emitted
    LAST, because every caller truncates this string with :func:`_tail`,
    which keeps the END. Put the verdict first and a busy download log erases
    it; put it last and it always survives.
    """
    err = " ".join(str(state.get("err") or "").split())
    lines = [" ".join(str(line).split())
             for line in (state.get("log") or [])
             if not _is_progress_line(line)]
    return " ".join(f"{' '.join(lines[-15:])} {err}".split())


def _mc_convert(rows_layers, plugin, out_dir: Path, resolutions, timeout: int,
                buildings=None, overwrite: bool = True,
                bbox: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """One REAL Map Converter run over a stack of catalogue rows.

    *rows_layers* is ``[(row, layer), …]`` in stack priority order (entry 0
    wins). *buildings* is an optional ``(row, layer)`` appended as a
    buildings entry. A plugin refusal in the main-thread resolve (the
    imagery guard, an unloaded layer, a failed render) propagates as
    ``RuntimeError`` — exactly what the GUI shows as a blocking dialog.

    Returns ``{"ok": output_dir|None, "err": message|None, "log": […],
    "tiles": [abt paths]}``.
    """
    mc = plugin["mc"]
    entries = [_layer_entry(row, layer, plugin, resolutions, bbox)
               for row, layer in rows_layers]
    if buildings is not None:
        brow, blayer = buildings
        entries.append(mc._LayerEntry(
            layer_type="buildings",
            source_path=blayer.source() if blayer is not None else brow["source"],
            qgis_layer=blayer,
            crs_authid=brow.get("crs") or "",
            extent=dict(bbox) if bbox else None))
    return _mc_run_entries(entries, plugin, out_dir, resolutions, timeout,
                           overwrite=overwrite)


def _mc_run_entries(entries, plugin, out_dir: Path, resolutions, timeout: int,
                    overwrite: bool = True) -> Dict[str, Any]:
    """The Map Converter's own resolve + worker over prebuilt entries.

    The tail every `_mc_convert` caller shares, exposed for the callers whose
    entry is not a catalogue row — the folder cases build the exact entry the
    GUI's Add Folder builds and hand it here.
    """
    mc = plugin["mc"]
    abt_mod = plugin["abt"]
    out_dir.mkdir(parents=True, exist_ok=True)
    _combined, render_jobs = mc._pending_render_jobs(
        entries, list(resolutions), str(out_dir), overwrite)
    resolved = mc.resolve_sources_with_progress(entries, None,
                                                render_jobs=render_jobs)
    if resolved is None:
        return {"ok": None, "err": "the resolve dialog reported cancelled — "
                                   "impossible headless, so this is a bug",
                "log": [], "tiles": []}
    worker = mc._MapConverterWorker(entries, str(out_dir), list(resolutions),
                                    resolved, overwrite=overwrite)
    state = _run_worker(worker, timeout)
    state["tiles"] = abt_mod.list_tiles(str(out_dir)) if state["ok"] else []
    return state


def _sa_analyse(row_label: str, layer, plugin, out_dir: Path, jobs, timeout: int,
                terrain_dir: str = "", osm_buildings: bool = False,
                results: Optional["Results"] = None) -> Dict[str, Any]:
    """One REAL Site Analysis run: the tab's preflight, then its worker.

    *jobs* is ``[(CoverageParams, display_name), …]`` — what the tab's
    _build_jobs produces from the user's tables. The tab's own hard gate
    (``antenna_height_error``) is enforced; its advisory prompts
    (``dem_layer_warning``, ``model_warnings``) are recorded as notes, the
    runner answering "run anyway" exactly like a determined user.
    """
    adapter = plugin["adapter"]
    for params, name in jobs:
        err = plugin["antenna_height_error"](params)
        if err:
            return {"ok": None, "err": f"the tab would refuse this job: {err}",
                    "log": []}
        if results is not None:
            for warning in plugin["model_warnings"](params):
                message = f"model warning — {getattr(warning, 'message', warning)}"
                # Once per unique message: the canonical torture parameters
                # trip the same two ITM advisories on every row, and 60
                # repeats bury the findings between them.
                if not any(message in note for note in results.notes):
                    results.note(f"{row_label} {name}: {message} "
                                 f"(same warning from later rows not repeated)")
    if layer is not None and results is not None:
        advisory = plugin["dem_layer_warning"](layer)
        if advisory:
            results.note(f"{row_label}: the tab would prompt — "
                         f"{_shorten(advisory, 100)} (answered: use it anyway)")
    out_dir.mkdir(parents=True, exist_ok=True)
    sat = plugin["sat"]
    worker = sat._SiteAnalysisWorker(list(jobs), layer, str(out_dir),
                                     terrain_dir or "",
                                     osm_buildings=osm_buildings)
    return _run_worker(worker, timeout)


def _coverage_params(plugin, lat: float, lon: float, resolution: int,
                     range_km: int, name: str, model: str = "ITM",
                     az: Tuple[float, float] = (0.0, 360.0)):
    """The canonical torture job — one place, so every run means the same."""
    return plugin["CoverageParams"](
        tx_lat=lat, tx_lon=lon, tx_height=30.0, tx_mode="AGL",
        freq_mhz=900.0, erp_watts=10.0, rx_height=2.0, rx_mode="AGL",
        model=model, resolution_m=resolution, max_range_km=range_km,
        backend="CPU", output_name=name, max_ram_gb=8, max_vram_gb=4,
        az_start=az[0], az_end=az[1])


def _sa_view_dir(source: str, lat: float, lon: float, range_km: float,
                 resolution: int, plugin,
                 az: Tuple[float, float] = (0.0, 360.0)) -> str:
    """Where prepare_terrain put this run's .abt view — the plugin's OWN
    cache identity, asked rather than guessed."""
    adapter = plugin["adapter"]
    bbox = adapter._compute_sector_bbox(lat, lon, range_km, az[0], az[1])
    subtiles = adapter._compute_subtiles(bbox, resolution, lat, lon, range_km,
                                         az[0], az[1])
    buildings_id = adapter.buildings_identity(None, False)
    return os.path.join(adapter.get_cache_dir(), adapter._VIEW_DIRNAME,
                        adapter._cache_key(source, subtiles, resolution,
                                           buildings_id))


def _pop_view_dirs(plugin) -> Optional[List[str]]:
    """The view directories ``prepare_terrain`` returned since the last pop.

    The mirror of ``pop_acquisition_routes`` (see :func:`_check_route`): the
    plugin RECORDS what it did and the runner reads the record. Recomputing
    the cache identity instead — which is what :func:`_sa_view_dir` does —
    means a runner whose guess has drifted reports "the Site Analysis run
    left no terrain view" about a view the plugin built perfectly well.

    ``None`` means this plugin copy predates the recorder, and the caller
    falls back to the recompute; ``[]`` means the recorder is there and
    ``prepare_terrain`` returned nothing, which is a different fact.
    """
    pop = getattr(plugin["adapter"], "pop_prepared_view_dirs", None)
    if pop is None:
        return None
    try:
        return [str(d) for d in (pop() or [])]
    except Exception:       # noqa: BLE001 — a diagnostic must not fail a run
        return None


def _sa_view_from_run(recorded: Optional[List[str]], source: str, lat: float,
                      lon: float, range_km: float, resolution: int, plugin,
                      az: Tuple[float, float] = (0.0, 360.0)
                      ) -> Tuple[str, str]:
    """``(view_dir, caveat)`` for the Site Analysis run that just finished.

    *recorded* is what :func:`_pop_view_dirs` returned, popped BEFORE the run
    and read after it, so the LAST entry is this run's view. The caveat is
    empty when the plugin told us (nothing to explain), and otherwise says
    which of the two other situations we are in — an older plugin copy, or a
    ``prepare_terrain`` that produced no directory at all. Those must never
    share a message: one is the runner not knowing, the other is the plugin
    not delivering.
    """
    if recorded is None:
        return (_sa_view_dir(source, lat, lon, range_km, resolution, plugin, az),
                "this plugin copy has no pop_prepared_view_dirs, so the "
                "directory above is the RUNNER's recomputed cache identity, "
                "not the one prepare_terrain returned")
    if not recorded:
        return "", "prepare_terrain returned no view directory"
    return recorded[-1], ""


#: Verdicts from :func:`_agree`.  ``DIFFER`` is a difference this suite
#: MEASURED; ``INCOMPARABLE`` is the absence of a measurement and must never be
#: read as one.  The buildings rows take "not the same" as proof that the
#: buildings were burned, so a two-valued answer let an unreadable or disjoint
#: pair of terrain directories pass as a successful burn.
SAME, DIFFER, INCOMPARABLE = "same", "differ", "incomparable"


def _bbox_mask(header, shape, bbox):
    """Pixels of a tile whose CENTRES fall inside a north/south/east/west box."""
    import numpy as np
    res = header.pixel_res
    lats = header.ul_lat - (np.arange(shape[0]) + 0.5) * res
    lons = header.ul_lon + (np.arange(shape[1]) + 0.5) * res
    rows = (lats <= bbox["north"]) & (lats >= bbox["south"])
    cols = (lons >= bbox["west"]) & (lons <= bbox["east"])
    return rows[:, None] & cols[None, :]


def _agree(a_dir, b_dir, plugin, bbox=None) -> Tuple[str, str]:
    """Do two terrain directories hold the same elevations? ``(verdict, why)``.

    With *bbox* the comparison is restricted to the pixels inside it — the
    ground a coverage over that box can actually see. A burn outside the box
    must NOT count as "the buildings changed the terrain": the coverage
    provably cannot react to it, and the pass would vouch for a delta the
    next check then measures as zero.
    """
    import numpy as np
    abt_mod = plugin["abt"]
    a = {Path(p).name: p for p in abt_mod.list_tiles(str(a_dir))}
    b = {Path(p).name: p for p in abt_mod.list_tiles(str(b_dir))}
    shared = sorted(set(a) & set(b))
    if not shared:
        return INCOMPARABLE, (f"no tile in common: one side wrote {sorted(a)[:2]}, "
                              f"the other {sorted(b)[:2]}")
    worst, n = 0.0, 0
    for name in shared:
        header_a = abt_mod.read_header(a[name])
        ga = abt_mod.read_tile(header_a)
        gb = abt_mod.read_tile(abt_mod.read_header(b[name]))
        if ga is None or gb is None or ga.shape != gb.shape:
            return INCOMPARABLE, f"{name}: unreadable or different shapes"
        mask = _real_mask(ga, plugin) & _real_mask(gb, plugin)
        if bbox is not None:
            mask &= _bbox_mask(header_a, ga.shape, bbox)
        if not mask.any():
            continue
        n += int(mask.sum())
        worst = max(worst, float(np.abs(ga[mask].astype("int32")
                                        - gb[mask].astype("int32")).max()))
    worst_m = worst * abt_mod.ELEV_STEP_M
    if not n:
        return INCOMPARABLE, "the two paths share no real sample to compare"
    if worst_m > 0.5:
        return DIFFER, (f"the two sides disagree by up to {worst_m:,.1f} m over "
                        f"{n:,} shared samples")
    return SAME, f"both sides agree to {worst_m:,.1f} m over {n:,} samples"


def _cross_tab_contract(mc_dir, sa_dir, plugin) -> Tuple[bool, str]:
    """The two tabs' terrain contract, stated in full and then measured.

    (a) They enumerate the SAME tiles — a tile only one side built is a
        divergence, not a footnote. (b) Where BOTH have data, the values are
        identical (the same converter wrote both). (c) Site Analysis never
        has LESS data than the Map Converter — its ``void_fill_m: 0.0``
        only ever adds. The old check compared the intersection only, so a
        path that dropped most of its terrain still "agreed".
    """
    import numpy as np
    abt_mod = plugin["abt"]
    a = {Path(p).name: p for p in abt_mod.list_tiles(str(mc_dir))}
    b = {Path(p).name: p for p in abt_mod.list_tiles(str(sa_dir))}
    if set(a) != set(b):
        only_mc = sorted(set(a) - set(b))[:3]
        only_sa = sorted(set(b) - set(a))[:3]
        return False, (f"the two tabs enumerated different tiles — "
                       f"only Map Converter: {only_mc}, only Site Analysis: {only_sa}")
    if not a:
        return False, "neither tab produced a tile"
    worst = 0.0
    shared_n = mc_real = sa_real = 0
    for name in sorted(a):
        ga = abt_mod.read_tile(abt_mod.read_header(a[name]))
        gb = abt_mod.read_tile(abt_mod.read_header(b[name]))
        if ga is None or gb is None or ga.shape != gb.shape:
            return False, f"{name}: unreadable or different shapes"
        mask_a = _real_mask(ga, plugin)
        mask_b = _real_mask(gb, plugin)
        mc_real += int(mask_a.sum())
        sa_real += int(mask_b.sum())
        both = mask_a & mask_b
        if both.any():
            shared_n += int(both.sum())
            worst = max(worst, float(np.abs(ga[both].astype("int32")
                                            - gb[both].astype("int32")).max()))
    worst_m = worst * plugin["abt"].ELEV_STEP_M
    if not shared_n:
        return False, "the two tabs share no real sample at all"
    if worst_m > 0.5:
        return False, (f"where both have data they disagree by up to "
                       f"{worst_m:,.1f} m over {shared_n:,} samples")
    if sa_real < mc_real:
        return False, (f"Site Analysis holds LESS terrain than the Map Converter "
                       f"({sa_real:,} vs {mc_real:,} real samples) — its fill "
                       f"only ever adds, so data was lost on the way")
    return True, (f"same {len(a)} tile(s), identical over {shared_n:,} shared "
                  f"samples, Site Analysis {sa_real:,} vs Map Converter "
                  f"{mc_real:,} real (fill only adds)")


#: Coincident-flank pixels that make an empty grid line a dropped WRITE
#: rather than terrain. Measured, not guessed: replaying aether_core's own
#: LOS sweep + polar->cartesian scatter over 15 real Swiss sites, the largest
#: flank coincidence a genuine terrain shadow produced was 2 pixels; the
#: smallest an injected dropped line produced was 9 (typically 35-94).
_STRIPE_QUORUM = 5


def _stripe_lines(line_valid, line_at, quorum: int = _STRIPE_QUORUM) -> List[int]:
    """Empty grid lines a write DROPPED, as opposed to terrain shadow.

    The valid-pixel bounding box alone is the wrong frame: the footprint is
    a DISK, so its outermost lines are short slivers a sparse LOS result
    legitimately leaves empty — and in LOS mode nodata does not even mean
    "not written" (the engine's 1-bit output writes 0 for shadow,
    out-of-range and never-computed alike). A dropped line is different in
    kind: the lines either side of the empty run still hold data at the very
    same positions, because they sample the same ground. So an empty run
    counts only when its two flanking lines are BOTH valid at ``quorum`` or
    more coincident positions.
    """
    import numpy as np
    n = len(line_valid)
    first = int(np.argmax(line_valid))
    last = n - 1 - int(np.argmax(line_valid[::-1]))
    flagged: List[int] = []
    k = first + 1
    while k < last:
        if line_valid[k]:
            k += 1
            continue
        j = k
        while not line_valid[j]:  # `last` is valid, so this terminates
            j += 1
        if int((line_at(k - 1) & line_at(j)).sum()) >= quorum:
            flagged.extend(range(k, j))
        k = j
    return flagged


def coverage_report(path: Path) -> Dict[str, Any]:
    """What the exported coverage GeoTIFF holds — values, grid and georef."""
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
    out: Dict[str, Any] = {
        "px": f"{width}x{height}", "width": width, "height": height,
        "valid": count,
        "valid_pct": round(100.0 * count / (width * height), 1),
        "bands": int(dataset.RasterCount),
        "geotransform": list(dataset.GetGeoTransform()),
        "projection": dataset.GetProjection() or "",
    }
    if count:
        out["min"] = round(float(values[mask].min()), 1)
        out["max"] = round(float(values[mask].max()), 1)
        out["mean"] = round(float(values[mask].mean()), 2)
        gap_row_idx = _stripe_lines(mask.any(axis=1), lambda k: mask[k, :])
        gap_col_idx = _stripe_lines(mask.any(axis=0), lambda k: mask[:, k])
        out["gap_rows"] = len(gap_row_idx)
        out["gap_cols"] = len(gap_col_idx)
        out["gap_row_idx"] = gap_row_idx
        out["gap_col_idx"] = gap_col_idx
    dataset = None
    return out


def _check_coverage(rid: str, tif: Optional[Path], params, row, results: Results,
                    plugin, prefix: str = "pipeline.cov") -> None:
    """The coverage the engine produced, held against its own request.

    Three checks, all planned up front: the GRID matches the job (size,
    georeferencing, one band, centred on the transmitter), the DISK is as
    full as the catalogue says a full-circle run over this terrain gets,
    and there are no STRIPES — an all-nodata row or column strictly inside
    the footprint is the literal shape of a half-written result. A missing
    *tif* fails all three by name; nothing is quietly withdrawn.
    """
    grid_id, disk_id, stripe_id = (f"{prefix}.grid", f"{prefix}.disk",
                                   f"{prefix}.stripes")
    if tif is None:
        for check_id, label in ((grid_id, "the coverage grid matches the job"),
                                (disk_id, "the coverage disk is as full as declared"),
                                (stripe_id, "the coverage has no stripes or gaps")):
            results.add(rid, check_id, label, FAIL,
                        "no coverage was produced to check (see the run above)",
                        known_fail_for(row, check_id))
        return
    cov = coverage_report(tif)

    # -- grid + georeferencing ------------------------------------------
    problems = []
    expected_px = 2.0 * params.max_range_km * 1000.0 / params.resolution_m
    for axis, got in (("width", cov["width"]), ("height", cov["height"])):
        if abs(got - expected_px) > max(5.0, expected_px * 0.05):
            problems.append(f"{axis} {got} px vs ~{expected_px:.0f} expected for "
                            f"{params.max_range_km} km @ {params.resolution_m} m")
    if cov["bands"] != 1:
        problems.append(f"{cov['bands']} bands (a coverage is one)")
    gt = cov["geotransform"]
    if abs(gt[1]) > 0:
        centre_lon = gt[0] + gt[1] * cov["width"] / 2.0
        centre_lat = gt[3] + gt[5] * cov["height"] / 2.0
        px_deg = abs(gt[5])
        if abs(centre_lat - params.tx_lat) > 6 * px_deg or \
           abs(centre_lon - params.tx_lon) > 6 * px_deg / max(
               0.2, math.cos(math.radians(params.tx_lat))):
            problems.append(f"centre {centre_lat:.4f},{centre_lon:.4f} is not the "
                            f"transmitter {params.tx_lat:.4f},{params.tx_lon:.4f}")
        wanted_deg = params.resolution_m / 111_111.0
        if not (0.7 * wanted_deg <= px_deg <= 1.4 * wanted_deg):
            problems.append(f"pixel height {px_deg:.6f}° vs ~{wanted_deg:.6f}° for "
                            f"{params.resolution_m} m")
    else:
        problems.append("no geotransform on the export at all")
    proj = cov["projection"]
    try:
        from osgeo import osr
        srs = osr.SpatialReference(wkt=proj)
        if not srs.IsGeographic():
            problems.append("the export's CRS is projected, not WGS84 degrees")
    except Exception:
        if "4326" not in proj:
            problems.append("the export carries no readable CRS")
    results.add(rid, grid_id, "the coverage grid matches the job",
                FAIL if problems else PASS,
                "; ".join(problems) if problems else
                f"{cov['px']} px, 1 band, WGS84, centred on the transmitter",
                known_fail_for(row, grid_id))

    # -- disk fullness ---------------------------------------------------
    band = list(row.get("expect_cov_valid_pct") or []) if row else []
    if len(band) == 2:
        lo, hi = float(band[0]), float(band[1])
        ok = lo <= cov["valid_pct"] <= hi
        results.add(rid, disk_id, "the coverage disk is as full as declared",
                    PASS if ok else FAIL,
                    f"{cov['valid_pct']}% valid vs the catalogue's "
                    f"{lo:.0f}–{hi:.0f}%"
                    + ("" if ok else " — a clipped or half-empty disk is a "
                                    "result nobody asked for"),
                    known_fail_for(row, disk_id))
    else:
        results.add(rid, disk_id, "the coverage disk is as full as declared",
                    FAIL, "the catalogue declares no expect_cov_valid_pct band "
                          "for this row — an unmeasured disk is not a pass",
                    known_fail_for(row, disk_id))

    # -- stripes ---------------------------------------------------------
    gaps = (cov.get("gap_rows") or 0) + (cov.get("gap_cols") or 0)
    results.add(rid, stripe_id, "the coverage has no stripes or gaps",
                PASS if cov.get("valid") and not gaps else FAIL,
                (f"{cov.get('gap_rows', '?')} dropped row(s) and "
                 f"{cov.get('gap_cols', '?')} dropped column(s) strictly inside "
                 f"the footprint" + _gap_lines_suffix(cov)
                 if cov.get("valid") else "no valid pixel at all"),
                known_fail_for(row, stripe_id))


def _check_result_loads(rid: str, state: Dict[str, Any], row, results: Results,
                        plugin, check_id: str = "pipeline.cov.load") -> None:
    """The exported result must load through the plugin's own loader.

    ``result_loader.load_coverage_result`` is what turns the engine's file
    into the styled layer the user sees; 800 lines of it previously had no
    pipeline coverage at all. Loaded, verified, and NOT left in the project.
    """
    loaded, problems = 0, []
    for tif_path, model, display_name in (state.get("ok") or []):
        try:
            layer = plugin["result_loader"].load_coverage_result(
                tif_path, model, display_name)
            if layer is None or not layer.isValid():
                problems.append(f"{display_name}: the loader returned an "
                                f"invalid layer")
            elif layer.renderer() is None:
                problems.append(f"{display_name}: loaded with no renderer/styling")
            else:
                loaded += 1
        except Exception as exc:
            problems.append(f"{display_name}: {type(exc).__name__}: "
                            f"{_shorten(str(exc), 90)}")
    results.add(rid, check_id, "the result loads through the plugin's own loader",
                PASS if loaded and not problems else FAIL,
                f"{loaded} result(s) loaded and styled"
                + ("; " + "; ".join(problems[:2]) if problems else ""),
                known_fail_for(row, check_id))


def _abt_asserts(rid: str, row, tiles, plugin, lat: float, lon: float,
                 results: Results, check_id: str, label: str,
                 note: str) -> bool:
    """Everything a terrain directory must prove before a coverage may run.

    Readable; not all-VOID; not constant; not download-failure fill (the
    plugin's own ``_abt_has_zero_fill`` plus a 0 m floor the catalogue's band
    contradicts); a REAL probe pixel at the transmitter (VOID there means
    the engine computes around a hole exactly where it matters most); the
    catalogue's elevation band; and the catalogue's real-fraction floor.
    Returns True when the terrain is sound.
    """
    adapter = plugin["adapter"]
    report = abt_report(tiles, plugin)
    if "unreadable" in report:
        results.add(rid, check_id, label, FAIL,
                    f"the engine wrote {report['unreadable']} and the plugin "
                    f"cannot read it", known_fail_for(row, check_id))
        return False
    if not report["real_px"]:
        results.add(rid, check_id, label, FAIL,
                    f"{note}, and every sample is VOID — the source contributed "
                    f"nothing and the run still said done",
                    known_fail_for(row, check_id))
        return False
    if report["min_m"] == report["max_m"]:
        results.add(rid, check_id, label, FAIL,
                    f"{note}, every real sample is {report['min_m']:,.1f} m — a "
                    f"constant tile is what a silently-failed source writes",
                    known_fail_for(row, check_id))
        return False

    problems = []
    band = row.get("expect_elev_m") or []
    at = abt_at(report["grids"], lat, lon, plugin)
    if at is None or at <= plugin["abt"].MIN_VALID_ELEV_M or \
            at == CONVERTER_VOID_COUNTS * plugin["abt"].ELEV_STEP_M:
        problems.append(f"the probe pixel at {lat:.4f},{lon:.4f} — the "
                        f"transmitter — is {'outside the tiles' if at is None else 'VOID'}")
    elif len(band) == 2 and not (float(band[0]) <= at <= float(band[1])):
        problems.append(f"{at:,.1f} m at the probe, outside the plausible "
                        f"{float(band[0]):,.0f}..{float(band[1]):,.0f} m")

    # Download-failure fill: the converter writes a FAILED tile as 0 m. The
    # plugin ships a detector for exactly that; the suite finally calls it.
    if len(band) == 2 and float(band[0]) > 0 and report["min_m"] == 0.0:
        problems.append(f"the tiles bottom out at exactly 0.0 m where the "
                        f"catalogue's floor is {float(band[0]):,.0f} m — that is "
                        f"download-failure fill, not terrain")
    try:
        gap_checker = getattr(adapter, "_abt_has_zero_fill", None)
        if gap_checker is not None:
            for path in tiles:
                if gap_checker(str(path)):
                    problems.append(f"{Path(path).name}: the plugin's own "
                                    f"zero-fill detector flags failure-fill "
                                    f"blocks (an old converter wrote these)")
                    break
    except Exception as exc:  # noqa: BLE001 — the detector must not hide a row
        problems.append(f"_abt_has_zero_fill could not run: {exc}")

    floor_pct = float(row.get("expect_real_pct") or 0.0)
    real_pct = 100.0 * report["real_px"] / max(report["total_px"], 1)
    if floor_pct and real_pct < floor_pct:
        problems.append(f"only {real_pct:.1f}% of samples are real vs the "
                        f"catalogue's {floor_pct:.0f}% floor")

    detail = (f"{note}, {report['real_px']:,}/{report['total_px']:,} real, "
              f"{report['min_m']:,.1f}..{report['max_m']:,.1f} m")
    if at is not None:
        detail += f", {at:,.1f} m at {lat:.4f},{lon:.4f}"
    results.add(rid, check_id, label, FAIL if problems else PASS,
                detail + ("; " + "; ".join(problems) if problems else ""),
                known_fail_for(row, check_id))
    return not problems


class _Reference:
    """The catalogue's reference terrain, and the tiles it was built with.

    A record rather than a bare path, because every row's agreement check
    has to be able to ask "is it still THERE?". On 2026-09-20 it was there
    for rows 2.2-2.8 and gone from 2.13 on, and the suite filed nine rows of
    "no shared real sample with the reference" without once saying that the
    thing they were held against had disappeared — nine accusations against
    nine innocent rows.
    """

    def __init__(self, directory: Path, tile_paths: Sequence[str]) -> None:
        self.dir = Path(directory)
        #: File names present when the reference was built and verified.
        self.names: List[str] = sorted(Path(p).name for p in tile_paths)
        #: The first row at which it was found missing, if ever.
        self.vanished_at: Optional[str] = None


def _reference_missing(reference: "_Reference", plugin) -> List[str]:
    """Reference tiles that are no longer present and readable.

    Headers only: a few bytes per tile, so this runs before every comparison
    without re-reading 62 MB of samples. A file that has lost its header has
    lost the comparison too.
    """
    abt_mod = plugin["abt"]
    if not reference.dir.is_dir():
        return list(reference.names)
    gone = []
    for name in reference.names:
        path = reference.dir / name
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                gone.append(name)
                continue
            if abt_mod.read_header(str(path)) is None:
                gone.append(name)
        except OSError:
            gone.append(name)
    return gone


def _check_agrees(rid: str, row, tiles, reference: Optional["_Reference"],
                  tolerance: float, results: Results, plugin) -> None:
    """This row's terrain against the catalogue's reference, over the same
    ground — p95 AND max over the samples BOTH sides call real.

    What this measures: wrong VALUES (a decode error, an offset, a shear).
    What it deliberately does not: missing samples — the mask intersection
    excludes them, so stripes and holes are the real-fraction floor's and
    the probe check's job, not this one's.

    The reference was built ONCE, by its own worker run, into its own
    directory OUTSIDE the per-row scratch tree; nothing here re-acquires
    anything, so the old aliasing (the reference overwriting the very tiles
    it was compared against) cannot recur. It is nevertheless re-verified
    before every comparison: "the reference is gone" and "this row's terrain
    is wrong" are opposite findings and must never share a message.
    """
    import numpy as np
    abt_mod = plugin["abt"]
    if reference is None:
        results.add(rid, "pipeline.agrees",
                    "agrees with the reference over the same ground", FAIL,
                    "the reference terrain was never built, so this row's "
                    "terrain was never independently measured",
                    known_fail_for(row, "pipeline.agrees"))
        return

    # -- is the thing we compare against still there? ---------------------
    gone = _reference_missing(reference, plugin)
    if gone:
        if reference.vanished_at is None:
            reference.vanished_at = rid
            results.note(
                f"the reference terrain vanished DURING the run: {len(gone)} "
                f"of {len(reference.names)} tile(s) under {reference.dir} are "
                f"missing or unreadable, first noticed at row {rid}. Every "
                f"agreement check from here on fails for that reason — not "
                f"because those rows' terrain is wrong")
        results.add(rid, "pipeline.agrees",
                    "agrees with the reference over the same ground", FAIL,
                    f"the reference terrain vanished during the run "
                    f"({reference.dir}) — {len(gone)} of "
                    f"{len(reference.names)} tile(s) missing or unreadable "
                    f"(first: {gone[0]}); first noticed at row "
                    f"{reference.vanished_at}",
                    known_fail_for(row, "pipeline.agrees"))
        return

    ref = {Path(p).name: p for p in abt_mod.list_tiles(str(reference.dir))}
    mine_names = [Path(p).name for p in tiles]
    overlap = sorted(set(ref) & set(mine_names))
    diffs = []
    samples = 0
    # Why each tile contributed nothing, so an empty comparison can be read.
    reasons: List[str] = []
    for path in tiles:
        name = Path(path).name
        other = ref.get(name)
        if other is None:
            reasons.append(f"{name}: no tile of that name in the reference")
            continue
        mine = abt_mod.read_tile(abt_mod.read_header(str(path)))
        theirs = abt_mod.read_tile(abt_mod.read_header(str(other)))
        if mine is None:
            reasons.append(f"{name}: this row's tile is unreadable ({path})")
            continue
        if theirs is None:
            reasons.append(f"{name}: the reference tile is unreadable ({other})")
            continue
        if mine.shape != theirs.shape:
            reasons.append(f"{name}: shapes {mine.shape} vs {theirs.shape}")
            continue
        mask = _real_mask(mine, plugin) & _real_mask(theirs, plugin)
        if not mask.any():
            reasons.append(f"{name}: no sample both sides call real")
            continue
        diffs.append(np.abs(mine[mask].astype("int32")
                            - theirs[mask].astype("int32")))
        samples += int(mask.sum())
    if not samples:
        results.add(rid, "pipeline.agrees",
                    "agrees with the reference over the same ground", FAIL,
                    f"no shared real sample with the reference — the "
                    f"comparison the catalogue declares could not be made. "
                    f"The reference holds {len(ref)} tile(s) at "
                    f"{reference.dir}, this row wrote {len(mine_names)}, "
                    f"{len(overlap)} name(s) in common"
                    + ("; " + "; ".join(reasons[:3]) if reasons else ""),
                    known_fail_for(row, "pipeline.agrees"))
        return
    step = abt_mod.ELEV_STEP_M
    all_diffs = np.concatenate(diffs)
    p95 = float(np.percentile(all_diffs, 95)) * step
    worst = float(all_diffs.max()) * step
    # max at 4x, not 2x: the 2026-08-26 licensed run measured genuine
    # cross-source single-pixel outliers of 51-60 m (steep Bern terrain,
    # swissALTI-derived fixtures vs AWS terrarium) at a p95 of 13-20 m —
    # honest data, not a defect. The genuine defect that day (1.3a) failed
    # on p95 alone at 555 m, so the max clause only needs to catch gross
    # localized corruption, not shave healthy outliers. A row whose
    # REFERENCE is known-imperfect on extreme slopes (arbitrated against a
    # third source) may carry its own ceiling in the catalogue
    # (expect_agrees_max_m); p95 keeps the real guard.
    max_allowed = float(row.get("expect_agrees_max_m") or 0.0) or 4.0 * tolerance
    # A row that raises its max also bounds the TAIL (expect_agrees_p999_m),
    # so sub-0.1% corruption cannot hide inside the reference's own cliff
    # error; p95 keeps the real guard either way.
    p999_allowed = float(row.get("expect_agrees_p999_m") or 0.0)
    p999 = float(np.percentile(all_diffs, 99.9)) * step
    ok = p95 <= tolerance and worst <= max_allowed and \
        (not p999_allowed or p999 <= p999_allowed)
    detail = (f"p95 |difference| {p95:,.1f} m, max {worst:,.1f} m over "
              f"{samples:,} shared samples (catalogue allows p95 "
              f"{tolerance:,.0f} m, max {max_allowed:,.0f} m)")
    if p999_allowed:
        detail += f"; p99.9 {p999:,.1f} m vs the catalogue's {p999_allowed:,.0f} m"
    results.add(rid, "pipeline.agrees",
                "agrees with the reference over the same ground",
                PASS if ok else FAIL,
                detail + ("" if ok else " — this source's terrain is wrong, "
                                        "not merely different"),
                known_fail_for(row, "pipeline.agrees"))


def _check_nothing_cached(row, rid, plugin, results: Results, pipe) -> None:
    """After a refusal, the run's cache must hold nothing a later run reuses.

    The row ran inside its own redirected cache (the Pipeline's), so whatever
    the refusal left behind is in there and nowhere else — pool, views, all
    of it is scanned. The old version asked ``_pool_dir(row["source"])``,
    whose identity never matched the live layer's source for file rows and
    does not exist at all for rendered ones, so it PASSed 7 of its 11 rows
    on the emptiness of a directory nothing had ever written.
    """
    abt_mod = plugin["abt"]
    left, reusable = [], []
    for path in sorted(Path(pipe.cache).rglob("*.abt")):
        left.append(path)
        grid = abt_mod.read_tile(abt_mod.read_header(str(path)))
        if grid is None:
            continue
        real = _real_mask(grid, plugin)
        if int(real.sum()):
            reusable.append((path.name, int(real.sum()), int(grid.size)))
    if not reusable:
        results.add(rid, "pipeline.nothing_cached", "the refusal cached nothing reusable",
                    PASS, f"{len(left)} .abt file(s) anywhere in the run's cache, "
                          f"none of them usable terrain",
                    known_fail_for(row, "pipeline.nothing_cached"))
        return
    name, real, total = reusable[0]
    results.add(rid, "pipeline.nothing_cached", "the refusal cached nothing reusable",
                FAIL,
                f"the failed run left {name} in the cache ({real:,}/{total:,} samples the "
                f"plugin calls terrain). The next run of this layer reports a cache hit, "
                f"skips the download and builds a coverage over it",
                known_fail_for(row, "pipeline.nothing_cached"))


def _check_tile_bytes(rid: str, row, tiles, resolution: int, manifest,
                      results: Results) -> None:
    """Every produced .abt must be byte-exact for its resolution.

    The size ladder (extent°, size px, bytes) is fixed per resolution and the
    manifest carries it (``tile_bytes``, from the generator's RES_TABLE) — the
    cheapest possible proof that a run produced what it says it did, and the
    check that catches a stride overflow or a half-written tile instantly.
    """
    expected = (manifest.get("tile_bytes") or {}).get(str(int(resolution)))
    if not expected:
        results.add(rid, "pipeline.tile_bytes",
                    f"tiles are byte-exact for {resolution} m", FAIL,
                    f"the catalogue's tile_bytes table has no entry for "
                    f"{resolution} m — an unmeasured size is not a pass")
        return
    wrong = []
    for path in tiles:
        try:
            size = os.path.getsize(path)
        except OSError:
            wrong.append(f"{Path(path).name}: unreadable")
            continue
        if size != int(expected):
            wrong.append(f"{Path(path).name}: {size:,} B vs {int(expected):,}")
    results.add(rid, "pipeline.tile_bytes",
                f"tiles are byte-exact for {resolution} m",
                FAIL if wrong else PASS,
                "; ".join(wrong[:3]) if wrong else
                f"{len(tiles)} tile(s) at exactly {int(expected):,} B",
                known_fail_for(row, "pipeline.tile_bytes"))


def _view_void_report(view_dir: str, plugin) -> Tuple[int, int]:
    """``(void_samples, total_samples)`` across a Site Analysis view."""
    import numpy as np
    abt_mod = plugin["abt"]
    voids = total = 0
    for path in abt_mod.list_tiles(str(view_dir)):
        grid = abt_mod.read_tile(abt_mod.read_header(path))
        if grid is None:
            continue
        total += int(grid.size)
        floor = abt_mod.MIN_VALID_ELEV_M / abt_mod.ELEV_STEP_M
        voids += int(((grid <= floor) | (grid == CONVERTER_VOID_COUNTS)).sum())
    return voids, total


def _pipeline_overrun_scenario(row, scenario, layer, plugin, pipe: "Pipeline",
                               manifest, results: Results, timeout: int) -> None:
    """One over-the-box scenario, end to end, through the shipped workers.

    The contract under test (2026-08-31): ground the source has no data for
    must read **0 m sea level in Site Analysis** (view filled, run green,
    warning in the terrain log) and **VOID in Map Converter output** — never
    a download error, never silent −5 km pits, never an unwarned sea.
    """
    rid = row["row"]
    sid = scenario.get("id", "?")
    adapter = plugin["adapter"]
    resolution = int(scenario.get("res_m") or 30)
    range_km = int(scenario.get("range_km") or 3)
    if scenario.get("center"):
        lat, lon = float(scenario["center"][0]), float(scenario["center"][1])
    else:
        lat, lon = _probe_for(row, manifest)
    outside = scenario.get("outside") or []
    inside = scenario.get("inside")
    warn_substring = (scenario.get("warn") or "sea level").lower()
    bbox = adapter.analysis_bbox(lat, lon, range_km)
    step = plugin["abt"].ELEV_STEP_M

    tap = _MessageLogTap()
    with tap:
        try:
            mc_state = _mc_convert([(row, layer)], plugin, pipe.scratch / "mc",
                                   [resolution], timeout * 3, bbox=bbox)
        except RuntimeError as exc:
            mc_state = {"ok": None, "err": str(exc), "log": [], "tiles": []}
        params = _coverage_params(plugin, lat, lon, resolution, range_km,
                                  f"ovr_{sid.replace('-', '_')}")
        _pop_view_dirs(plugin)      # only THIS run's view may be read back
        sa_state = _sa_analyse(f"{rid} {sid}", layer, plugin,
                               pipe.scratch / "sa", [(params, sid)],
                               timeout * 3, results=results)
        sa_views = _pop_view_dirs(plugin)

    mc_ok = mc_state["ok"] is not None and bool(mc_state["tiles"])
    sa_ok = bool(sa_state["ok"])
    results.add(rid, f"overrun:{sid}.run", "the over-the-box run completes",
                PASS if mc_ok and sa_ok else FAIL,
                (f"{scenario.get('note', '')}".strip() or "both tabs ran")
                if mc_ok and sa_ok else
                "Map Converter: " + (_tail(_worker_complaint(mc_state) or "ok", 120)
                                     if not mc_ok else "ok")
                + "; Site Analysis: "
                + (_tail(" ".join((sa_state.get("err") or "").split()), 120)
                   if not sa_ok else "ok")
                + " — an over-the-box run must degrade to sea, not error",
                known_fail_for(row, f"overrun:{sid}.run"))

    said = tap.text().lower()
    warned = warn_substring in said
    results.add(rid, f"overrun:{sid}.warned",
                "the terrain log warns about sea fill",
                PASS if warned else FAIL,
                f"the log carries {warn_substring!r}" if warned else
                f"no {warn_substring!r} anywhere in the terrain log — a user "
                f"whose result is part sea must be told",
                known_fail_for(row, f"overrun:{sid}.warned"))

    # -- Map Converter output keeps VOID over the missing ground ----------
    if not mc_ok:
        results.add(rid, f"overrun:{sid}.mc_void",
                    "the Map Converter keeps voids there", FAIL,
                    "no Map Converter output to check (see the run above)",
                    known_fail_for(row, f"overrun:{sid}.mc_void"))
    else:
        report = abt_report(mc_state["tiles"], plugin)
        problems = []
        if "unreadable" in report:
            problems.append(f"unreadable tile {report['unreadable']}")
        else:
            at_out = abt_at(report["grids"], float(outside[0]),
                            float(outside[1]), plugin) \
                if len(outside) == 2 else None
            if len(outside) == 2 and at_out is None:
                problems.append(f"the outside probe {outside} is not inside "
                                f"the produced tiles — the scenario is "
                                f"miswritten, which must not pass")
            elif len(outside) == 2 and at_out > plugin["abt"].MIN_VALID_ELEV_M:
                problems.append(f"{at_out:,.1f} m at the outside probe — the "
                                f"Map Converter must keep no-data as VOID, "
                                f"not invent ground")
            if inside:
                at_in = abt_at(report["grids"], float(inside[0]),
                               float(inside[1]), plugin)
                if at_in is None or at_in <= plugin["abt"].MIN_VALID_ELEV_M:
                    problems.append(f"the inside probe {inside} is "
                                    f"{'outside the tiles' if at_in is None else 'VOID'}"
                                    f" — covered ground was lost")
        results.add(rid, f"overrun:{sid}.mc_void",
                    "the Map Converter keeps voids there",
                    FAIL if problems else PASS,
                    "; ".join(problems) if problems else
                    f"VOID at the outside probe"
                    + (", real ground at the inside probe" if inside else "")
                    + f" ({report.get('real_px', 0):,}/{report.get('total_px', 0):,} real)",
                    known_fail_for(row, f"overrun:{sid}.mc_void"))

    # -- Site Analysis view: sea outside, terrain inside, no voids at all -
    view, caveat = _sa_view_from_run(sa_views, layer.source(), lat, lon,
                                     float(range_km), resolution, plugin)
    if not view or not os.path.isdir(view):
        results.add(rid, f"overrun:{sid}.sea",
                    "Site Analysis reads 0 m outside the source", FAIL,
                    (caveat or "prepare_terrain returned no view directory")
                    if not view else
                    f"there is no terrain view at {view} (the run failed "
                    f"before terrain, or the cache identity moved)"
                    + (f" — {caveat}" if caveat else ""),
                    known_fail_for(row, f"overrun:{sid}.sea"))
    else:
        abt_mod = plugin["abt"]
        view_tiles = abt_mod.list_tiles(view)
        grids = [(abt_mod.read_header(p), abt_mod.read_tile(abt_mod.read_header(p)))
                 for p in view_tiles]
        grids = [(h, g) for h, g in grids if g is not None]
        problems = []
        voids, total = _view_void_report(view, plugin)
        if voids:
            problems.append(f"{voids:,}/{total:,} samples in the ENGINE's view "
                            f"are still void — aether_core would read them as "
                            f"terrain {CONVERTER_VOID_COUNTS * step:,.1f} m")
        if len(outside) == 2:
            at_out = abt_at(grids, float(outside[0]), float(outside[1]), plugin)
            if at_out is None:
                problems.append(f"the outside probe {outside} is not inside "
                                f"the view tiles")
            elif at_out != 0.0:
                problems.append(f"{at_out:,.1f} m at the outside probe — the "
                                f"contract is exactly 0 m (sea level)")
        if inside:
            at_in = abt_at(grids, float(inside[0]), float(inside[1]), plugin)
            if at_in is None or at_in <= 0.0:
                problems.append(f"the inside probe {inside} reads "
                                f"{'nothing' if at_in is None else f'{at_in:,.1f} m'}"
                                f" — covered ground must stay real terrain")
        results.add(rid, f"overrun:{sid}.sea",
                    "Site Analysis reads 0 m outside the source",
                    FAIL if problems else PASS,
                    "; ".join(problems) if problems else
                    f"view filled: 0 m at the outside probe, no void reaches "
                    f"the engine ({total:,} samples)",
                    known_fail_for(row, f"overrun:{sid}.sea"))

    # -- the coverage the user actually gets ------------------------------
    tif = None
    if sa_state["ok"]:
        tif_path = sa_state["ok"][0][0]
        tif = Path(tif_path) if os.path.isfile(tif_path) else None
    if tif is None:
        results.add(rid, f"overrun:{sid}.cov",
                    "the coverage over it is a sound grid", FAIL,
                    "no coverage was produced (see the run above)",
                    known_fail_for(row, f"overrun:{sid}.cov"))
    else:
        ok, why = _sound_grid(coverage_report(tif), range_km, resolution)
        results.add(rid, f"overrun:{sid}.cov",
                    "the coverage over it is a sound grid",
                    PASS if ok else FAIL, why,
                    known_fail_for(row, f"overrun:{sid}.cov"))


def _check_void_sentinel(plugin, results: Results) -> None:
    """The plugin's "is this terrain?" rule must reject the engine's own VOID.

    ``aether_converter`` writes -9999 COUNTS (-4999.5 m) for a pixel no source
    covered. The floor's HISTORY is why this check exists: it once sat one
    count below the sentinel, so ``src > MIN_VALID_ELEV_M`` called every hole
    valid ground instead of no-data. The check computes from the live
    constants, whatever their current values, so a regression reopens it
    loudly.
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


#: Rows whose catalogue ``check`` says the pipeline must NOT produce terrain.
_MUST_FAIL = ("error",)
#: Rows the plugin's own router must refuse before any terrain is fetched.
_MUST_REJECT = ("reject",)


def _probe_for(row, manifest) -> Tuple[float, float]:
    """Transmitter site for a row's pipeline run.

    The catalogue's elevation probe anchors every pipeline over the same
    ground whenever the row's footprint contains it — the coverage bands
    are calibrated there, and the buildings fixtures sit around it (a
    footprint-centre transmitter put the 1x1-degree rows 50 km into the
    High Alps, where a Bern building burn can never move a coverage
    pixel). Footprints that do not contain the probe run at their centre.
    """
    probe = manifest.get("elevation_probe") or {}
    lat = float(probe.get("lat", 46.945))
    lon = float(probe.get("lon", 7.41))
    box = row.get("footprint") or []
    if len(box) == 4 and not (box[0] <= lon <= box[2] and box[1] <= lat <= box[3]):
        return (box[1] + box[3]) / 2.0, (box[0] + box[2]) / 2.0
    return lat, lon


def _agrees_applies(row, manifest, plugin) -> bool:
    """Does the catalogue's reference comparison apply to this row's run?

    Structural, and measured in TILES rather than degrees: the catalogue
    declares a tolerance, the row runs at the reference's 30 m, and the
    row's enumerated tile set shares at least one tile with the
    reference's. A degree-radius gate silently excluded 2.8, whose AOI
    centre is 0.10 deg west of the probe and whose single tile is
    nevertheless the reference tile itself.
    """
    if float(row.get("expect_agrees_m") or 0.0) <= 0:
        return False
    if int(row.get("pipeline_res_m") or 30) != 30:
        return False
    if row["row"] == manifest.get("reference_row"):
        return False
    return bool(_row_tiles(row, manifest, plugin) & _reference_tiles(manifest, plugin))


def _row_tiles(row, manifest, plugin) -> set:
    mc = plugin["mc"]
    adapter = plugin["adapter"]
    lat, lon = _probe_for(row, manifest)
    bbox = adapter.analysis_bbox(lat, lon, int(row.get("pipeline_range_km") or 3))
    return {t["filename"] for _r, t in
            mc._enumerate_tiles(mc._snap_bbox(bbox, [30]), [30])}


def _reference_tiles(manifest, plugin) -> set:
    mc = plugin["mc"]
    adapter = plugin["adapter"]
    probe = manifest.get("elevation_probe") or {}
    lat = float(probe.get("lat", 46.945))
    lon = float(probe.get("lon", 7.41))
    bbox = adapter.analysis_bbox(lat, lon, 3)
    return {t["filename"] for _r, t in
            mc._enumerate_tiles(mc._snap_bbox(bbox, [30]), [30])}


def _plugin_identity() -> Tuple[str, str]:
    """``(path, version)`` of the waveshed package the imports resolved to."""
    import waveshed
    pkg = Path(waveshed.__file__).resolve().parent
    version = "unknown"
    try:
        for line in (pkg / "metadata.txt").read_text(encoding="utf-8").splitlines():
            if line.startswith("version="):
                version = line.split("=", 1)[1].strip()
                break
    except OSError:
        pass
    return str(pkg), version


#: Everything this runner drives on the plugin. A pairing that lacks one of
#: these would otherwise die as an AttributeError mid-run — which on
#: 2026-08-26 meant one crash line and 283 NOTRUNs.
_REQUIRED_PLUGIN_SURFACE = (
    ("mc", ("_pending_render_jobs", "_union_extent", "_detect_resolution",
            "resolve_sources_with_progress", "_MapConverterWorker",
            "_enumerate_tiles", "_snap_bbox", "_LayerEntry")),
    ("sat", ("_SiteAnalysisWorker",)),
    ("p2p", ("_P2PWorker", "_write_temp_batch_csv")),
    ("adapter", ("prepare_terrain", "analysis_bbox", "_compute_sector_bbox",
                 "_compute_subtiles", "_cache_key", "buildings_identity",
                 "_abt_has_zero_fill", "_abt_has_holes", "ensure_pool_tiles",
                 "pop_acquisition_routes", "pop_prepared_view_dirs")),
    ("result_loader", ("load_coverage_result",)),
)


def _pairing_problem(loaded_pkg: str, repo: Optional[Path], plugin) -> Optional[str]:
    """Why this runner must not test the waveshed it imported, or None.

    Tier C's subject is THIS repo's plugin: the runner and the workers it
    drives are one change set. So the import must resolve inside the repo —
    a standalone run guarantees that, while the QGIS console's preloaded
    installed copy silently wins every import — and the copy must carry
    every function the runner calls.
    """
    if repo is not None:
        loaded = Path(loaded_pkg).resolve()
        try:
            inside = loaded.is_relative_to(repo.resolve())
        except AttributeError:                      # Python < 3.9
            inside = str(loaded).startswith(str(repo.resolve()))
        if not inside:
            return (f"the imported waveshed lives at {loaded_pkg}, not under this "
                    f"repo ({repo}) — that is the installed/loaded copy, which this "
                    f"runner cannot vouch for. Run tier C standalone from the repo; "
                    f"to refresh the installed plugin, run deploy.py and restart QGIS")
    missing = []
    for key, names in _REQUIRED_PLUGIN_SURFACE:
        module = plugin.get(key)
        missing += [f"{key}.{name}" for name in names
                    if not hasattr(module, name)]
    if missing:
        return (f"the imported plugin lacks {', '.join(missing[:4])}"
                + (f" (+{len(missing) - 4} more)" if len(missing) > 4 else "")
                + " — plugin and runner are from different change sets; "
                  "bring both to the same checkout (deploy.py + restart QGIS, "
                  "or git pull the missing half)")
    return None


def _check_plugin_pairing(plugin, results: Results) -> bool:
    """The FIRST tier-C check: which plugin is under test, and is it ours.

    Runs before anything else is planned, deliberately: on a bad pairing the
    run FAILS here with one line instead of declaring ~280 checks whose only
    possible fate is a wall of NOTRUNs. Aborting before declaring is not a
    silent skip — the run is red and says exactly why nothing else ran.
    """
    results.plan("-", "plugin.pairing")
    loaded, version = _plugin_identity()
    problem = _pairing_problem(loaded, REPO, plugin)
    results.add("-", "plugin.pairing", "the plugin under test is this repo's",
                FAIL if problem else PASS,
                problem or f"waveshed {version} at {loaded}")
    return problem is None


def _plan_pipeline_row(row, manifest, results: Results, plugin) -> None:
    """Declare EVERY check this row's pipeline will file, before it runs.

    The accounting rule this rewrite exists for: a check that is declared
    and does not execute is a NOTRUN, and a NOTRUN fails the run. Nothing
    may decide mid-flight that a check "did not apply".
    """
    rid = row["row"]
    check = row.get("check")
    results.plan(rid, "pipeline")
    if check in _MUST_REJECT:
        results.plan(rid, "pipeline.sa_refuses")
        results.plan(rid, "pipeline.nothing_cached")
        return
    if check in _MUST_FAIL:
        results.plan(rid, "pipeline.nothing_cached")
        return
    if row.get("kind") == "vector":
        if check == "buildings":
            results.plan(rid, "pipeline.cov_delta")
        if rid.startswith("5.3"):
            # The links' verdicts: the obstructed link must cost materially
            # more than the clear one, per the layer's own expect_obstructed.
            results.plan(rid, "pipeline.verdicts")
        return
    if check == "both" and row.get("kind") == "raster":
        results.plan(rid, "pipeline.sa")
        results.plan(rid, "pipeline.both_tabs")
        results.plan(rid, "pipeline.tile_bytes")
        if row.get("expect_acquire"):
            results.plan(rid, "pipeline.route")
        if _agrees_applies(row, manifest, plugin):
            results.plan(rid, "pipeline.agrees")
        for suffix in ("grid", "disk", "stripes", "load"):
            results.plan(rid, f"pipeline.cov.{suffix}")
        # The boundary scenarios — declared like everything else, so a
        # scenario that never executes is a NOTRUN, not a vanished check.
        for scenario in row.get("overrun") or []:
            sid = scenario.get("id", "?")
            results.plan(rid, f"overrun:{sid}.run")
            results.plan(rid, f"overrun:{sid}.warned")
            results.plan(rid, f"overrun:{sid}.sea")
            results.plan(rid, f"overrun:{sid}.mc_void")
            results.plan(rid, f"overrun:{sid}.cov")


def _not_an_overrun(check_id: str) -> bool:
    """True for every check of a row EXCEPT its boundary scenarios.

    The ``overrun:`` checks are driven by their own loop in
    :func:`tier_pipeline`, AFTER the row driver has returned — so a row
    driver bailing out early has not skipped them and must not fail them.
    """
    return not check_id.startswith("overrun:")


def _overrun_checks_of(sid: str) -> Callable[[str], bool]:
    """Selects the checks belonging to ONE boundary scenario, for _fail_rest."""
    prefix = f"overrun:{sid}."
    return lambda check_id: check_id.startswith(prefix)


def _fail_rest(rid: str, row: Optional[Dict[str, Any]], results: Results,
               why: str,
               only: Optional[Callable[[str], bool]] = _not_an_overrun) -> int:
    """File every still-planned check of *rid* as FAIL. Returns how many.

    This module's rule (see :func:`_plan_pipeline_row`) is that a declared
    check ends in a counted status. An early return that files only the
    top-level ``pipeline`` verdict leaves the rest of the row unexecuted —
    on 2026-09-20 that was 8 NOTRUNs per row from two returns inside
    :func:`_pipeline_row`, holes the runner opened in its own accounting.

    Every remaining check is therefore failed BY NAME with the reason it was
    never reached, which is strictly more information than "declared by the
    manifest, never executed": the report says which check the row owed and
    what stopped it. *only* narrows the set (see :func:`_not_an_overrun`).
    """
    filed = 0
    for planned_row, check_id in list(results.planned):
        if planned_row != rid or (rid, check_id) in results.executed:
            continue
        if only is not None and not only(check_id):
            continue
        results.add(rid, check_id, check_id, FAIL, f"not reached: {why}",
                    known_fail_for(row or {}, check_id))
        filed += 1
    return filed


def _check_route(rid: str, row, mc_routes: List[str], sa_routes: List[str],
                 results: Results) -> None:
    """Terrain must arrive by the route the catalogue intends.

    A fallback that still produced terrain is a FAILED test, not a passed
    one — unless the fallback IS the row's intention, in which case the
    catalogue's ``expect_acquire`` says so. Routes come from the plugin's
    own ``pop_acquisition_routes``: "download" = the shared Rust XYZ
    downloader, "render" = per-tile QGIS export of a rendered server,
    "sources" = local files handed to the converter.
    """
    expected = (row.get("expect_acquire") or "").strip()
    if not expected:
        results.unplan(rid, "pipeline.route")
        return
    problems = []
    for phase, routes in (("Map Converter", mc_routes),
                          ("Site Analysis", sa_routes)):
        got = sorted(set(routes))
        if not got:
            problems.append(f"{phase} recorded no acquisition at all — the "
                            f"plugin surface moved, or nothing was fetched")
        elif got != [expected]:
            problems.append(f"{phase} acquired via {'+'.join(got)}")
    results.add(rid, "pipeline.route",
                f"terrain arrives the intended way ({expected})",
                FAIL if problems else PASS,
                ("; ".join(problems) + " — a silent fallback is a failure, "
                 "not a pass") if problems else
                f"both tabs acquired via {expected}",
                known_fail_for(row, "pipeline.route"))


def _pipeline_row(row, layer, plugin, pipe: "Pipeline", manifest, results,
                  timeout: int, reference: Optional["_Reference"]) -> None:
    """One row, end to end, through the shipped workers.

    Every ``return`` below goes through :func:`_fail_rest` first: the checks
    this row declared are its debt, and a driver that walks away from them
    leaves NOTRUN holes rather than a verdict.
    """
    rid = row["row"]
    if layer is None:
        results.add(rid, "pipeline", "terrain out of this layer", FAIL,
                    "the layer is not in the project, so nothing can be run through it")
        _fail_rest(rid, row, results,
                   "the layer is not in the project")
        return
    if row.get("kind") == "vector":
        _pipeline_vector_row(row, layer, plugin, pipe, manifest, results, timeout)
        # A vector row has no boundary scenarios, so anything still planned
        # here is a hole one of the vector drivers left.
        _fail_rest(rid, row, results,
                   "the row's vector driver returned without filing it")
        return

    resolution = int(row.get("pipeline_res_m") or 30)
    range_km = int(row.get("pipeline_range_km") or 3)
    lat, lon = _probe_for(row, manifest)
    adapter = plugin["adapter"]
    bbox = adapter.analysis_bbox(lat, lon, range_km)
    must_fail = row.get("check") in _MUST_FAIL
    must_reject = row.get("check") in _MUST_REJECT
    _pop_routes = getattr(adapter, "pop_acquisition_routes", lambda: [])
    _pop_routes()  # drop whatever an earlier phase recorded

    # ---- Map Converter, the real one -----------------------------------
    mc_out = pipe.scratch / "mc"
    try:
        state = _mc_convert([(row, layer)], plugin, mc_out, [resolution],
                            timeout, bbox=bbox)
    except RuntimeError as exc:
        state = {"ok": None, "err": str(exc), "log": [], "tiles": []}
    mc_routes = _pop_routes()
    said = _worker_complaint(state)

    if must_reject:
        wanted = (row.get("expect_failure") or "").strip().lower()
        if state["ok"] is not None:
            results.add(rid, "pipeline", "the plugin refuses to run this as terrain",
                        FAIL, f"it did not refuse — {len(state['tiles'])} tile(s) were "
                              f"produced from a layer the catalogue says must never be "
                              f"offered as terrain", known_fail_for(row, "pipeline"))
        elif _is_synthesized(said) or (wanted and wanted not in said.lower()):
            results.add(rid, "pipeline", "the plugin refuses to run this as terrain",
                        FAIL, f"it refused, but not for the catalogue's reason "
                              f"({wanted!r}): {_tail(said, 110)} — a network hiccup "
                              f"or a runner timeout must not count as the classifier "
                              f"working", known_fail_for(row, "pipeline"))
        else:
            results.add(rid, "pipeline", "the plugin refuses to run this as terrain",
                        PASS, _tail(said, 110), known_fail_for(row, "pipeline"))
        _reject_sa_check(row, layer, plugin, pipe, results, lat, lon,
                         resolution, range_km, timeout)
        _check_nothing_cached(row, rid, plugin, results, pipe)
        _fail_rest(rid, row, results,
                   "the row ended at the must-reject verdict above")
        return

    if must_fail:
        if state["ok"] is not None:
            results.add(rid, "pipeline", "the pipeline fails, loudly and for the right reason",
                        FAIL, f"it succeeded — {len(state['tiles'])} tile(s). The catalogue "
                              f"says this source must be refused, and terrain nobody can "
                              f"trust is worse than none", known_fail_for(row, "pipeline"))
        elif _is_synthesized(said):
            results.add(rid, "pipeline", "the pipeline fails, loudly and for the right reason",
                        FAIL, f"the runner's own timeout/cancel is not a refusal: "
                              f"{_tail(said, 110)}", known_fail_for(row, "pipeline"))
        else:
            ok, why = _refusal_is_the_right_one(row, said, row["source"], 1)
            results.add(rid, "pipeline", "the pipeline fails, loudly and for the right reason",
                        PASS if ok else FAIL, why, known_fail_for(row, "pipeline"))
        _check_nothing_cached(row, rid, plugin, results, pipe)
        _fail_rest(rid, row, results,
                   "the row ended at the must-fail verdict above")
        return

    if row.get("check") != "both":
        # Planned as bare `pipeline` only — a raster row with an unknown
        # check must fail HERE, not wander into the both-row flow and file
        # six checks nobody declared.
        results.add(rid, "pipeline", "this layer drives a real run", FAIL,
                    f"no pipeline role is defined for check={row.get('check')!r}")
        _fail_rest(rid, row, results,
                   f"no pipeline role for check={row.get('check')!r}")
        return
    if state["ok"] is None:
        results.add(rid, "pipeline", "terrain out of this layer, through the real Map Converter",
                    FAIL, _tail(said, 240), known_fail_for(row, "pipeline"))
        _fail_rest(rid, row, results,
                   f"the Map Converter run failed ({_tail(said, 120)})")
        return
    tiles = state["tiles"]
    expected_names = {t["filename"] for _r, t in plugin["mc"]._enumerate_tiles(
        plugin["mc"]._snap_bbox(bbox, [resolution]), [resolution])}
    got_names = {Path(t).name for t in tiles}
    if got_names != expected_names:
        results.add(rid, "pipeline",
                    "terrain out of this layer, through the real Map Converter",
                    FAIL, f"the run produced tiles {sorted(got_names)[:3]} where the "
                          f"plugin's own enumeration says {sorted(expected_names)[:3]} "
                          f"— a dropped or extra tile, before values were even read",
                    known_fail_for(row, "pipeline"))
        _fail_rest(rid, row, results,
                   f"the Map Converter produced a tile set the plugin's own "
                   f"enumeration does not match ({len(got_names)} tile(s) vs "
                   f"{len(expected_names)})")
        return
    _check_tile_bytes(rid, row, tiles, resolution, manifest, results)
    terrain_ok = _abt_asserts(rid, row, tiles, plugin, lat, lon, results,
                              "pipeline",
                              "terrain out of this layer, through the real Map Converter",
                              f"{len(tiles)} tile(s) via the Map Converter worker "
                              f"in {state.get('elapsed', 0.0):.0f}s")

    # ---- the catalogue's independent reference -------------------------
    if _agrees_applies(row, manifest, plugin):
        _check_agrees(rid, row, tiles, reference,
                      float(row.get("expect_agrees_m") or 0.0), results, plugin)

    # ---- Site Analysis, the real one, end to end -----------------------
    params = _coverage_params(plugin, lat, lon, resolution, range_km,
                              f"cov_{rid.replace('.', '_')}")
    sa_out = pipe.scratch / "sa"
    _pop_view_dirs(plugin)      # only THIS run's view may be read back
    sa_state = _sa_analyse(rid, layer, plugin, sa_out,
                           [(params, f"torture {rid}")], timeout * 3,
                           results=results)
    sa_routes = _pop_routes()
    sa_views = _pop_view_dirs(plugin)
    if sa_state["ok"] is not None:
        results.add(rid, "pipeline.sa", "a real Site Analysis run, end to end",
                    PASS, f"{len(sa_state['ok'])} result(s) out of the worker "
                          f"in {sa_state.get('elapsed', 0.0):.0f}s",
                    known_fail_for(row, "pipeline.sa"))
    else:
        results.add(rid, "pipeline.sa", "a real Site Analysis run, end to end",
                    FAIL, _tail(" ".join((sa_state["err"] or "").split()), 320),
                    known_fail_for(row, "pipeline.sa"))
    _check_route(rid, row, mc_routes, sa_routes, results)

    # ---- the two tabs against each other -------------------------------
    sa_view, caveat = _sa_view_from_run(sa_views, layer.source(), lat, lon,
                                        float(range_km), resolution, plugin)
    if sa_view and os.path.isdir(sa_view):
        ok, why = _cross_tab_contract(mc_out, sa_view, plugin)
        results.add(rid, "pipeline.both_tabs",
                    "Site Analysis and the Map Converter build the same terrain",
                    PASS if ok else FAIL,
                    why + (f" — {caveat}" if caveat else ""),
                    known_fail_for(row, "pipeline.both_tabs"))
    elif not sa_view:
        results.add(rid, "pipeline.both_tabs",
                    "Site Analysis and the Map Converter build the same terrain",
                    FAIL, caveat or "prepare_terrain returned no view directory",
                    known_fail_for(row, "pipeline.both_tabs"))
    else:
        results.add(rid, "pipeline.both_tabs",
                    "Site Analysis and the Map Converter build the same terrain",
                    FAIL, f"there is no terrain view at {sa_view} to compare "
                          f"(the run failed before terrain, or the cache "
                          f"identity moved)" + (f" — {caveat}" if caveat else ""),
                    known_fail_for(row, "pipeline.both_tabs"))

    # ---- the coverage itself -------------------------------------------
    tif = None
    if sa_state["ok"]:
        tif_path = sa_state["ok"][0][0]
        tif = Path(tif_path) if os.path.isfile(tif_path) else None
    _check_coverage(rid, tif, params, row, results, plugin)
    _check_result_loads(rid, sa_state, row, results, plugin)
    if not terrain_ok:
        results.note(f"{rid}: the coverage checks above ran over terrain that "
                     f"already failed its own asserts — fix the terrain first")


def _reject_sa_check(row, layer, plugin, pipe, results, lat, lon, resolution,
                     range_km, timeout) -> None:
    """A reject row must be refused on the Site Analysis surface too.

    The Map Converter refusing is half the story: Site Analysis, P2P and
    both Processing algorithms go through ``prepare_terrain``, which had no
    guard at all until 2026-08. The worker must end in an error that names
    the classification, with no terrain and no coverage behind it.
    """
    rid = row["row"]
    params = _coverage_params(plugin, lat, lon, resolution, range_km,
                              f"rej_{rid.replace('.', '_')}")
    state = _sa_analyse(rid, layer, plugin, pipe.scratch / "sa_reject",
                        [(params, f"reject {rid}")], timeout)
    said = " ".join((state["err"] or "").split())
    wanted = (row.get("expect_failure") or "").strip().lower()
    if state["ok"] is not None:
        results.add(rid, "pipeline.sa_refuses",
                    "Site Analysis refuses it too", FAIL,
                    "the Site Analysis worker ran it as terrain — the refusal "
                    "exists only in the Map Converter",
                    known_fail_for(row, "pipeline.sa_refuses"))
    elif _is_synthesized(said) or (wanted and wanted not in said.lower()):
        results.add(rid, "pipeline.sa_refuses",
                    "Site Analysis refuses it too", FAIL,
                    f"it failed, but not for the catalogue's reason ({wanted!r}): "
                    f"{_shorten(said, 110)}",
                    known_fail_for(row, "pipeline.sa_refuses"))
    else:
        results.add(rid, "pipeline.sa_refuses",
                    "Site Analysis refuses it too", PASS, _shorten(said, 110),
                    known_fail_for(row, "pipeline.sa_refuses"))


# ---------------------------------------------------------------------------
# Rows that are not themselves terrain
# ---------------------------------------------------------------------------

def _pipeline_vector_row(row, layer, plugin, pipe, manifest, results, timeout):
    rid = row["row"]
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
    """Buildings must change the terrain AND the coverage over it.

    Both through the real workers: two Map Converter runs (bare stack, and
    the same stack plus this buildings layer) prove the burn changed the
    surface; two Site Analysis runs over those two terrain directories
    prove the change reaches the number the user reads. "It was accepted"
    proves nothing; "the dB moved" is the product.
    """
    rid = row["row"]
    picked = _pick_roles(manifest)
    dem_row = picked.get("dem")
    layers = _project_layers()
    dem_layer = layers.get(dem_row["name"]) if dem_row else None
    if dem_row is None or dem_layer is None:
        results.add(rid, "pipeline", "buildings change the terrain", FAIL,
                    "the catalogue has no loadable local DEM to burn them into")
        results.add(rid, "pipeline.cov_delta", "buildings change the coverage",
                    FAIL, "no DEM, so no coverage either")
        return
    lat, lon = _probe_for(dem_row, manifest)
    adapter = plugin["adapter"]
    bbox = adapter.analysis_bbox(lat, lon, 3)

    bare = _mc_convert([(dem_row, dem_layer)], plugin, pipe.scratch / "bare",
                       [30], timeout, bbox=bbox)
    built = _mc_convert([(dem_row, dem_layer)], plugin, pipe.scratch / "built",
                        [30], timeout, buildings=(row, layer), bbox=bbox)
    if bare["ok"] is None or built["ok"] is None:
        results.add(rid, "pipeline", "buildings change the terrain", FAIL,
                    f"a converter run failed before the comparison: "
                    f"{_tail((bare['err'] or built['err'] or ''), 180)}",
                    known_fail_for(row, "pipeline"))
        results.add(rid, "pipeline.cov_delta", "buildings change the coverage",
                    FAIL, "no terrain pair to run over",
                    known_fail_for(row, "pipeline.cov_delta"))
        return
    # Restricted to the analysis bbox: a burn the coverage cannot see must
    # not pass as "the buildings changed the terrain".
    verdict, why = _agree(pipe.scratch / "bare", pipe.scratch / "built", plugin,
                          bbox=bbox)
    detail = f"burning {Path(layer.source().partition('|')[0]).name} into {dem_row['row']} "
    if verdict == INCOMPARABLE:
        detail += f"could not be measured against the bare surface: {why}"
    elif verdict == SAME:
        detail += (f"changed nothing inside the analysis box, and no warning "
                   f"was raised. {_buildings_shape(layer.source())}")
    else:
        detail += f"raised the surface inside the analysis box ({why.split(' over ')[0]})"
    results.add(rid, "pipeline", "buildings change the terrain",
                PASS if verdict == DIFFER else FAIL, detail,
                known_fail_for(row, "pipeline"))

    # The coverage delta — real Site Analysis runs over the two terrain
    # directories (the GUI's own flow for file buildings: Map Converter
    # builds the terrain, Site Analysis runs over it as a terrain dir).
    params = _coverage_params(plugin, lat, lon, 30, 3,
                              f"bld_{rid.replace('.', '_')}")
    run_a = _sa_analyse(rid, dem_layer, plugin, pipe.scratch / "cov_bare",
                        [(params, "bare")], timeout * 3,
                        terrain_dir=str(pipe.scratch / "bare"))
    run_b = _sa_analyse(rid, dem_layer, plugin, pipe.scratch / "cov_built",
                        [(params, "built")], timeout * 3,
                        terrain_dir=str(pipe.scratch / "built"))
    if not (run_a["ok"] and run_b["ok"]):
        results.add(rid, "pipeline.cov_delta", "buildings change the coverage",
                    FAIL, f"a coverage run failed: "
                          f"{_tail((run_a['err'] or run_b['err'] or ''), 180)}",
                    known_fail_for(row, "pipeline.cov_delta"))
        return
    import numpy as np
    from osgeo import gdal
    gdal.UseExceptions()
    va = gdal.Open(run_a["ok"][0][0]).ReadAsArray()
    vb = gdal.Open(run_b["ok"][0][0]).ReadAsArray()
    if va is None or vb is None or va.shape != vb.shape:
        # An unreadable or mismatched pair is NOT "0 pixels moved" — that
        # message accuses the product of a defect the runner never measured.
        results.add(rid, "pipeline.cov_delta", "buildings change the coverage",
                    FAIL,
                    f"the two coverages could not be compared: "
                    f"{'unreadable result' if va is None or vb is None else f'shapes {va.shape} vs {vb.shape}'}",
                    known_fail_for(row, "pipeline.cov_delta"))
        return
    changed = int((va != vb).sum())
    results.add(rid, "pipeline.cov_delta", "buildings change the coverage",
                PASS if changed else FAIL,
                f"{changed:,} coverage pixel(s) moved when the buildings went in"
                + ("" if changed else " — the burn reached the terrain but not "
                                      "the number the user reads"),
                known_fail_for(row, "pipeline.cov_delta"))


def _pipeline_sites(row, layer, plugin, pipe, manifest, results, timeout):
    """EVERY site runs as a transmitter — each on terrain over its own ground.

    The catalogue scatters the sites deliberately (Bern, the Dead Sea, the
    Andes, Fiji). The old check ran only the ones the Bern reference box
    happened to cover and passed with two thirds of the layer untested.
    Now each site gets its own Site Analysis run over the catalogue's
    global reference service; a site that cannot run fails the row.
    """
    rid = row["row"]
    by_row = {r["row"]: r for r in manifest["rows"]}
    ref = by_row.get(manifest.get("reference_row"))
    layers = _project_layers()
    dem_layer = layers.get(ref["name"]) if ref else None
    if dem_layer is None:
        results.add(rid, "pipeline", "every site runs as a transmitter", FAIL,
                    "the catalogue's reference service is not in the project, so "
                    "no site has terrain to run over")
        return
    ran, failed = 0, []
    names = layer.fields().names()
    for index, feature in enumerate(layer.getFeatures()):
        point = feature.geometry().asPoint()
        label = str(feature["name"]) if "name" in names else f"site {index}"
        # The site's OWN height and mode — they are the attributes' reason to
        # exist. Running every site at a canonical 30 m AGL meant the -430 m
        # AMSL entry (the Dead Sea shore, the catalogue's negative-AMSL case)
        # was never once run as AMSL by this suite.
        height = 30.0
        mode = "AGL"
        try:
            if "height_m" in names and feature["height_m"] is not None:
                height = float(feature["height_m"])
            if "mode" in names and feature["mode"]:
                mode = str(feature["mode"]).strip().upper() or "AGL"
        except (TypeError, ValueError):
            failed.append(f"{label}: unreadable height_m/mode attributes")
            continue
        params = plugin["CoverageParams"](
            tx_lat=point.y(), tx_lon=point.x(), tx_height=height, tx_mode=mode,
            freq_mhz=900.0, erp_watts=10.0, rx_height=2.0, rx_mode="AGL",
            model="ITM", resolution_m=30, max_range_km=3, backend="CPU",
            output_name=f"site_{index}", max_ram_gb=8, max_vram_gb=4)
        state = _sa_analyse(rid, dem_layer, plugin,
                            pipe.scratch / f"site_{index}",
                            [(params, f"{label} ({height:g} m {mode})")],
                            timeout * 3)
        if state["ok"]:
            ran += 1
        else:
            failed.append(f"{label}: {_tail(' '.join((state['err'] or '').split()), 120)}")
    detail = (f"{ran} of {ran + len(failed)} site(s) produced a coverage on its "
              f"own ground, each with its own height/mode")
    if failed:
        detail += "; " + "; ".join(failed[:2])
    results.add(rid, "pipeline", "every site runs as a transmitter",
                PASS if ran and not failed else FAIL, detail,
                known_fail_for(row, "pipeline"))


def _pipeline_links(row, layer, plugin, pipe, manifest, results, timeout):
    """EVERY link runs as a real P2P job, through the P2P tab's own worker.

    The batch CSV comes from ``p2p_tab._write_temp_batch_csv`` and the run
    from ``p2p_tab._P2PWorker`` — terrain, job file, licensed engine and
    output land exactly where the tab puts them. Links far from Bern get
    terrain over their own ground from the catalogue's reference service.
    """
    rid = row["row"]
    p2p = plugin["p2p"]
    by_row = {r["row"]: r for r in manifest["rows"]}
    ref = by_row.get(manifest.get("reference_row"))
    layers = _project_layers()
    dem_layer = layers.get(ref["name"]) if ref else None
    if dem_layer is None:
        results.add(rid, "pipeline", "every link runs as a P2P job", FAIL,
                    "the catalogue's reference service is not in the project")
        # pipeline.verdicts is planned alongside this one; bailing out here
        # left it a NOTRUN.
        _fail_rest(rid, row, results,
                   "the catalogue's reference service is not in the project")
        return
    ran, failed = 0, []
    fields = layer.fields().names()
    verdicts: Dict[str, Dict[str, Any]] = {}
    for index, feature in enumerate(layer.getFeatures()):
        points = feature.geometry().asPolyline()
        if len(points) < 2:
            failed.append(f"link {index}: fewer than two endpoints")
            continue
        csv_path = p2p._write_temp_batch_csv(
            points[0].y(), points[0].x(), 30.0, "AGL",
            points[-1].y(), points[-1].x(), 2.0, "AGL")
        params = plugin["P2PParams"](
            tx_lat=points[0].y(), tx_lon=points[0].x(), tx_height=30.0,
            tx_mode="AGL", freq_mhz=900.0, erp_watts=10.0, rx_height=2.0,
            rx_mode="AGL", model="ITM", resolution_m=30, max_range_km=60,
            backend="CPU", output_name=f"p2p_{index}", max_ram_gb=8,
            max_vram_gb=4)
        worker = p2p._P2PWorker(params, dem_layer,
                                str(pipe.scratch / f"p2p_{index}"), csv_path)
        state = _run_worker(worker, timeout * 3)
        try:
            os.remove(csv_path)
        except OSError:
            pass
        if state["ok"]:
            ran += 1
            expect = (feature["expect_obstructed"]
                      if "expect_obstructed" in fields else None)
            if expect is not None and str(expect) not in ("", "NULL"):
                verdicts[str(feature["name"]) if "name" in fields
                         else f"link {index}"] = {
                    "obstructed": str(expect).lower() in ("true", "1"),
                    "csv": os.path.join(str(state["ok"]), f"p2p_{index}.csv"),
                    "a": (points[0].y(), points[0].x()),
                    "b": (points[-1].y(), points[-1].x()),
                }
        else:
            failed.append(f"link {index}: {_tail(' '.join((state['err'] or '').split()), 120)}")
    detail = f"{ran} of {ran + len(failed)} link(s) ran over their own ground"
    if failed:
        detail += "; " + "; ".join(failed[:2])
    results.add(rid, "pipeline", "every link runs as a P2P job",
                PASS if ran and not failed else FAIL, detail,
                known_fail_for(row, "pipeline"))
    _check_link_verdicts(rid, row, verdicts, results)


#: ``Path_Loss_dB`` the engine writes when it has NO result for a link. It is
#: a sentinel, not a 9,999 dB path: treating it as a number made the excess
#: over free space come out at roughly +9,880 dB or, after the clear/
#: obstructed subtraction, a nonsense negative margin.
_NO_RESULT_PATH_LOSS_DB = 9999.0

#: How far under free space a REAL result may legitimately sit. Free space is
#: the theoretical floor for a single path, but two-ray ground reflection can
#: add up to ~6 dB of constructive interference at the receiver, so anything
#: within 6 dB is honest physics and anything below it is not a propagation
#: result at all.
_BELOW_FREE_SPACE_TOLERANCE_DB = 6.0


def _excess_loss_db(csv_path: str, a: Tuple[float, float],
                    b: Tuple[float, float], freq_mhz: float = 900.0
                    ) -> Tuple[Optional[float], str]:
    """``(loss - free_space, detail)`` from an engine P2P result CSV.

    ``Path_Loss_dB`` is a frozen, named column (CONTRACT §5c). Free-space at
    the same distance and frequency is exact arithmetic, so the difference is
    pure terrain effect — which is the only number an obstruction verdict can
    stand on without re-implementing the model.
    """
    import csv as _csv
    try:
        with open(csv_path, "r", encoding="utf-8") as fh:
            rows = list(_csv.DictReader(fh))
    except OSError as exc:
        return None, f"cannot read {os.path.basename(csv_path)}: {exc}"
    if not rows or "Path_Loss_dB" not in (rows[0] or {}):
        return None, (f"{os.path.basename(csv_path)} carries no Path_Loss_dB "
                      f"column (got {sorted((rows[0] or {}).keys()) if rows else 'no rows'})")
    try:
        loss = float(rows[0]["Path_Loss_dB"])
    except (TypeError, ValueError):
        return None, f"Path_Loss_dB is not a number: {rows[0]['Path_Loss_dB']!r}"
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    d_km = 2 * 6371.0 * math.asin(math.sqrt(h))
    fspl = 32.44 + 20 * math.log10(max(d_km, 1e-3)) + 20 * math.log10(freq_mhz)

    # A value the engine never computed is not a measurement, and subtracting
    # free space from it produces a confident, meaningless "excess". Row 5.3
    # reported a large NEGATIVE excess on 2026-09-20 — which reads as "the
    # obstructed link is cheaper than free space" — because the sentinel and
    # the sub-free-space cases were never separated from a real number.
    if loss >= _NO_RESULT_PATH_LOSS_DB - 0.5:
        return None, (f"Path_Loss_dB is {loss:g} — the "
                      f"{_NO_RESULT_PATH_LOSS_DB:g} sentinel: the engine "
                      f"reported no result for this {d_km:.1f} km link")
    if loss < fspl - _BELOW_FREE_SPACE_TOLERANCE_DB:
        return None, (f"Path_Loss_dB {loss:.1f} dB is below free space "
                      f"({fspl:.1f} dB over {d_km:.1f} km, tolerance "
                      f"{_BELOW_FREE_SPACE_TOLERANCE_DB:g} dB) — physically "
                      f"impossible, so this is not a usable result")
    return loss - fspl, f"{loss:.1f} dB over {d_km:.1f} km (free space {fspl:.1f})"


def _check_link_verdicts(rid: str, row, verdicts: Dict[str, Dict[str, Any]],
                         results: Results) -> None:
    """The obstructed link must cost materially more than the clear one.

    Relative, not absolute: ITM adds climate and terrain terms even to a
    clear path, so a fixed excess-loss threshold would measure the model's
    constants. What the catalogue's two Bern links exist to prove is the
    ORDER — Bern->Thun (a ridge in the way) versus Bern->Gurten (line of
    sight) — and 6 dB is well under any real knife-edge diffraction loss
    while well over numeric noise.
    """
    obstructed = {n: v for n, v in verdicts.items() if v["obstructed"]}
    clear = {n: v for n, v in verdicts.items() if not v["obstructed"]}
    if not obstructed or not clear:
        results.add(rid, "pipeline.verdicts",
                    "the obstructed link costs more than the clear one", FAIL,
                    f"the layer declares {len(obstructed)} obstructed and "
                    f"{len(clear)} clear link(s) that ran — the comparison "
                    f"needs one of each", known_fail_for(row, "pipeline.verdicts"))
        return
    problems, measured = [], []
    worst_clear = None
    for name, v in clear.items():
        excess, detail = _excess_loss_db(v["csv"], v["a"], v["b"])
        if excess is None:
            problems.append(f"{name}: {detail}")
            continue
        measured.append(f"{name}: excess {excess:+.1f} dB")
        worst_clear = excess if worst_clear is None else max(worst_clear, excess)
    best_obstructed = None
    for name, v in obstructed.items():
        excess, detail = _excess_loss_db(v["csv"], v["a"], v["b"])
        if excess is None:
            problems.append(f"{name}: {detail}")
            continue
        measured.append(f"{name}: excess {excess:+.1f} dB")
        best_obstructed = (excess if best_obstructed is None
                           else min(best_obstructed, excess))
    if problems:
        results.add(rid, "pipeline.verdicts",
                    "the obstructed link costs more than the clear one", FAIL,
                    "; ".join(problems[:2]),
                    known_fail_for(row, "pipeline.verdicts"))
        return
    margin = best_obstructed - worst_clear
    ok = margin >= 6.0
    results.add(rid, "pipeline.verdicts",
                "the obstructed link costs more than the clear one",
                PASS if ok else FAIL,
                "; ".join(measured) + f" — margin {margin:+.1f} dB"
                + ("" if ok else " (< 6 dB: the model does not see the ridge "
                                 "between Bern and Thun, or sees one on the "
                                 "clear path)"),
                known_fail_for(row, "pipeline.verdicts"))


#: Attributes the converter's height ladder reads off a building feature, in
#: the order it tries them. Kept in step with `aether_converter`'s own ladder
#: (`buildings.rs` HeightSource) — the point of the message is to say what the
#: converter could have used, so a stale list understates the fixture.
_BUILDING_HEIGHT_FIELDS = ("height", "render_height", "building:height",
                           "levels", "building:levels", "building_levels")


def _buildings_shape(uri: str) -> str:
    """What a buildings source actually carries, for the failure message.

    *uri* is the LAYER uri, not a plain path: a ``…gpkg|layername=buildings``
    source must be inspected as the sublayer the row selected. Opening the
    container and taking layer 0 made rows 3.2a and 3.2b — which exist to prove
    the two sublayers land in different halves of the tile — print byte-
    identical details.
    """
    try:
        from osgeo import ogr
        path, _, rest = uri.partition("|")
        wanted = next((part.split("=", 1)[1] for part in rest.split("|")
                       if part.startswith("layername=")), None)
        source = ogr.Open(path)
        layer = source.GetLayerByName(wanted) if wanted else source.GetLayer(0)
        if layer is None:
            return f"(the source has no layer named {wanted!r})"
        names = {layer.GetLayerDefn().GetFieldDefn(i).GetName()
                 for i in range(layer.GetLayerDefn().GetFieldCount())}
        usable = [n for n in _BUILDING_HEIGHT_FIELDS if n in names]
        total = layer.GetFeatureCount()
        heighted = sum(1 for f in layer
                       if any(f.GetField(n) not in (None, "") for n in usable))
        has_z = ogr.GT_HasZ(layer.GetGeomType())
        return (f"Source: layer {layer.GetName()!r}, {total:,} features, "
                f"{heighted:,} carrying one of {usable or '(no height field)'}, "
                f"geometry {'has' if has_z else 'has NO'} Z (absolute roof elevation).")
    except Exception as exc:
        return f"(the source could not be inspected: {type(exc).__name__})"


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
    total = sum(1 for _f in layer.getFeatures())
    results.add(row["row"], "pipeline", "every footprint is where its raster really is",
                PASS if checked and not wrong else FAIL,
                f"{checked} of {total} footprints match the plugin's own bounds"
                + (f" ({total - checked} are remote/vsi sources with no local "
                   f"file to compare)" if total != checked else "")
                + ("; " + "; ".join(wrong[:3]) if wrong else ""),
                known_fail_for(row, "pipeline"))


# ---------------------------------------------------------------------------
# Map Converter stacks, and the run-level matrix
# ---------------------------------------------------------------------------

#: The Map Converter stack combinations the catalogue implies but no single
#: row can express. Each names the ROLE a row must play; the rows themselves
#: are picked from the manifest, so this list never goes stale against it.
#: ``priority`` proves both halves of the sources[] contract with SOLO runs:
#: the stack equals the top source where it has data, and equals the base
#: where the top has none — so a converter that drops the second source, or
#: takes them in the wrong order, or blends, fails by measurement.
_COMBINATIONS = (
    {"name": "files + files (priority order)", "roles": ("dem2", "dem"),
     "resolutions": (30,), "asserts": "priority"},
    {"name": "file over xyz (mixed acquisition)", "roles": ("dem2", "xyz"),
     "resolutions": (30,), "asserts": "priority"},
    {"name": "xyz over file (reversed priority)", "roles": ("xyz", "dem"),
     "resolutions": (30,), "asserts": "priority_top"},
    {"name": "rendered service under files", "roles": ("rendered", "dem"),
     "resolutions": (30,), "asserts": "inert_top"},
    {"name": "two resolutions in one run", "roles": ("dem",),
     "resolutions": (30, 90), "asserts": "multires"},
    {"name": "files + buildings", "roles": ("dem",), "buildings": True,
     "resolutions": (30,), "asserts": "burn"},
    {"name": "rerun with overwrite off", "roles": ("dem",),
     "resolutions": (30,), "asserts": "skip_existing"},
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
    # dem must COVER the probe tile (a full source is the only honest base),
    # dem2 must NOT (a partial top is the only way both halves of the
    # priority contract are measurable). expect_real_pct is the catalogue's
    # own statement of each fixture's coverage.
    full = [r for r in dems if float(r.get("expect_real_pct") or 0) >= 90.0]
    partial = [r for r in dems if 0 < float(r.get("expect_real_pct") or 0) < 90.0]
    dems = (full[:1] + partial[:1]) if full and partial else dems
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
        picked["dem2"] = dems[1]          # the partial one, when both exist
    if xyz:
        picked["xyz"] = xyz[0]
    if rendered:
        picked["rendered"] = rendered[0]
    if buildings:
        picked["buildings"] = buildings[0]
    return picked


def _stack_follows(stack_tiles, top_dir, base_dir, plugin,
                   require_base: bool = True) -> Tuple[bool, str]:
    """Both halves of the priority contract, by measurement against solos.

    Where the TOP solo has data the stack must equal it exactly; where the
    top has none and the BASE solo does, the stack must equal the base.
    With *require_base* (the default) a run in which the base never got to
    contribute FAILS — "takes the base's 0 samples" is not a measurement,
    and a converter that drops the second source entirely sailed through
    exactly that hole. ``require_base=False`` is for the one deliberate
    top-covers-everything case, and says so in its verdict.
    """
    import numpy as np
    abt_mod = plugin["abt"]
    top = {Path(p).name: p for p in abt_mod.list_tiles(str(top_dir))}
    base = {Path(p).name: p for p in abt_mod.list_tiles(str(base_dir))}
    top_n = base_n = 0
    for path in stack_tiles:
        name = Path(path).name
        s = abt_mod.read_tile(abt_mod.read_header(str(path)))
        if s is None:
            return False, f"{name}: the stack tile is unreadable"
        t = (abt_mod.read_tile(abt_mod.read_header(top[name]))
             if name in top else None)
        b = (abt_mod.read_tile(abt_mod.read_header(base[name]))
             if name in base else None)
        mask_t = _real_mask(t, plugin) if t is not None else None
        if mask_t is not None and mask_t.any():
            if int(np.abs(s[mask_t].astype("int32")
                          - t[mask_t].astype("int32")).max()) > 0:
                return False, (f"{name}: the stack does not follow its "
                               f"highest-priority source where it has data")
            top_n += int(mask_t.sum())
        if b is not None:
            mask_b = _real_mask(b, plugin)
            if mask_t is not None:
                mask_b &= ~mask_t
            if mask_b.any():
                if int(np.abs(s[mask_b].astype("int32")
                              - b[mask_b].astype("int32")).max()) > 0:
                    return False, (f"{name}: where the top source has no data "
                                   f"the base's samples were not used — the "
                                   f"second source is dropped or corrupted")
                base_n += int(mask_b.sum())
    if not top_n:
        return False, "the top source contributed no sample at all — priority untestable"
    if require_base and not base_n:
        return False, ("the top source covers every sample, so the base's "
                       "contribution was never measured — this combination "
                       "cannot prove the second source is read at all")
    if base_n:
        return True, (f"follows the top source over {top_n:,} samples and takes "
                      f"the base's {base_n:,} where the top has none")
    return True, (f"follows the top source over {top_n:,} samples (top covers "
                  f"everything — priority half only; contribution is proved by "
                  f"the sibling combination)")


def _identical_terrain(a_dir, b_dir, plugin) -> Tuple[bool, str]:
    """Are two terrain directories EXACTLY the same — tiles, values, voids?

    The inert-top contract: an empty top source must leave the base as its
    solo run built it, byte for byte. Mask-intersection agreement (:func:`_agree`)
    cannot say that — it ignores every sample only one side calls real.
    """
    import numpy as np
    abt_mod = plugin["abt"]
    a = {Path(p).name: p for p in abt_mod.list_tiles(str(a_dir))}
    b = {Path(p).name: p for p in abt_mod.list_tiles(str(b_dir))}
    if set(a) != set(b):
        return False, f"different tile sets ({sorted(set(a) ^ set(b))[:3]})"
    if not a:
        return False, "no tiles on either side"
    # The sample total is accumulated HERE, inside the guarded loop. Summing
    # it afterwards re-read every tile a second time (2x62 MB on row 1.10)
    # through an unguarded ``.size`` — and ``read_tile`` returns None for an
    # unreadable tile, so that line raised AttributeError and took the whole
    # check down instead of failing it. An unreadable tile is a FAILED
    # comparison, named by its path.
    total = 0
    for name in sorted(a):
        ga = abt_mod.read_tile(abt_mod.read_header(a[name]))
        gb = abt_mod.read_tile(abt_mod.read_header(b[name]))
        if ga is None:
            return False, f"{name}: unreadable ({a[name]})"
        if gb is None:
            return False, f"{name}: unreadable ({b[name]})"
        if ga.shape != gb.shape or not np.array_equal(ga, gb):
            diff = int((ga != gb).sum()) if ga.shape == gb.shape else -1
            return False, (f"{name}: {diff:,} sample(s) differ (voids included)"
                           if diff >= 0 else f"{name}: different shapes")
        total += int(ga.size)
    return True, f"{len(a)} tile(s), {total:,} samples equal, voids included"


def _tier_pipeline_combinations(manifest, results, layers, plugin,
                                scratch_root: Path, timeout: int,
                                keep: bool = False) -> None:
    """Run the Map Converter over STACKS, through the real worker.

    A converter that handles every source in isolation and drops the second
    one, or takes them in the wrong priority order, or builds only the first
    of two requested resolutions, passes every per-row check in this file.
    These are the cases only a stack can express — and each one is proved by
    solo runs and pixel measurement, never by counting job-file entries.
    """
    adapter = plugin["adapter"]
    picked = _pick_roles(manifest)
    probe = manifest.get("elevation_probe") or {}
    lat, lon = float(probe.get("lat", 46.945)), float(probe.get("lon", 7.41))

    for combo in _COMBINATIONS:
        label = combo["name"]
        check_id = f"mc:{label}"
        missing = [r for r in combo["roles"] if r not in picked]
        if combo.get("buildings") and "buildings" not in picked:
            missing.append("buildings")
        if missing:
            results.add("mc", check_id, label, FAIL,
                        f"the catalogue has no row to play {', '.join(missing)} — "
                        f"a combination that cannot be cast is a hole, not a skip")
            continue
        scratch = scratch_root / ("mc_" + re.sub(r"[^a-z0-9]+", "_", label.lower()))
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            with Pipeline(scratch, keep) as pipe:
                _run_combination(combo, label, check_id, picked, layers, plugin,
                                 pipe, adapter, lat, lon, results, timeout)
        except Exception as exc:
            results.add("mc", check_id, label, FAIL,
                        f"{type(exc).__name__}: {' '.join(str(exc).split())[:170]}")


def _run_combination(combo, label, check_id, picked, layers, plugin, pipe,
                     adapter, lat, lon, results, timeout) -> None:
    resolutions = list(combo["resolutions"])
    bbox = adapter.analysis_bbox(lat, lon, 3)
    stack = [(picked[role], layers.get(picked[role]["name"]))
             for role in combo["roles"]]
    if any(layer is None for _row, layer in stack):
        results.add("mc", check_id, label, FAIL,
                    "a role's layer is not in the project")
        return
    buildings = None
    if combo.get("buildings"):
        brow = picked["buildings"]
        blayer = layers.get(brow["name"])
        if blayer is None:
            results.add("mc", check_id, label, FAIL,
                        "the buildings layer is not in the project")
            return
        buildings = (brow, blayer)

    out = pipe.scratch / "stack"
    state = _mc_convert(stack, plugin, out, resolutions, timeout * 3,
                        buildings=buildings, bbox=bbox)
    if state["ok"] is None:
        results.add("mc", check_id, label, FAIL,
                    f"the worker failed: {_tail(state['err'] or '', 200)}")
        return
    if not state["tiles"]:
        results.add("mc", check_id, label, FAIL,
                    "the worker reported done and produced no tile")
        return
    report = abt_report(state["tiles"], plugin)
    if not report.get("real_px"):
        results.add("mc", check_id, label, FAIL,
                    f"{len(state['tiles'])} tile(s) written and every sample is VOID")
        return
    # The same sanity every row's terrain gets — the saboteur proved a
    # constant-500 m converter passed three combos green while printing
    # "500.0..500.0 m" in its own detail line.
    if report["min_m"] == report["max_m"]:
        results.add("mc", check_id, label, FAIL,
                    f"every real sample is {report['min_m']:,.1f} m — a constant "
                    f"tile is what a silently-failed source writes, not terrain")
        return
    at = abt_at(report["grids"], lat, lon, plugin)
    if at is None or at == CONVERTER_VOID_COUNTS * plugin["abt"].ELEV_STEP_M:
        results.add("mc", check_id, label, FAIL,
                    f"the probe pixel at {lat:.4f},{lon:.4f} is "
                    f"{'outside the tiles' if at is None else 'VOID'}")
        return
    detail = (f"{len(stack)} entr(ies) -> {len(state['tiles'])} tile(s), "
              f"{report['min_m']:,.1f}..{report['max_m']:,.1f} m")

    def solo(role: str) -> Optional[Path]:
        row = picked[role]
        solo_out = pipe.scratch / f"solo_{role}"
        try:
            solo_state = _mc_convert([(row, layers.get(row["name"]))], plugin,
                                     solo_out, resolutions, timeout * 3,
                                     bbox=bbox)
        except RuntimeError:
            return None
        return solo_out if solo_state["ok"] is not None else None

    asserts = combo["asserts"]
    if asserts in ("priority", "priority_top"):
        top_dir = solo(combo["roles"][0])
        base_dir = solo(combo["roles"][1])
        if top_dir is None or base_dir is None:
            results.add("mc", check_id, label, FAIL,
                        "a solo reference run failed, so priority was never measured")
            return
        ok, why = _stack_follows(state["tiles"], top_dir, base_dir, plugin,
                                 require_base=(asserts == "priority"))
        if not ok:
            results.add("mc", check_id, label, FAIL, why)
            return
        detail += f"; {why}"
    elif asserts == "inert_top":
        # The top source has no data over this ground (the rendered service's
        # footprint is elsewhere) — the contract is that an empty top leaves
        # the base EXACTLY as its solo run built it.
        base_dir = solo(combo["roles"][1])
        if base_dir is None:
            results.add("mc", check_id, label, FAIL,
                        "the base solo run failed, so nothing was measured")
            return
        # EXACT equality, voids included. _agree intersects the real masks,
        # so a stack with half its samples VOIDed "agreed to 0.0 m" with a
        # pristine base — the saboteur walked straight through that.
        ok, why = _identical_terrain(base_dir, out, plugin)
        if not ok:
            results.add("mc", check_id, label, FAIL,
                        f"stacking an empty top source changed the base: {why}")
            return
        detail += f"; empty top left the base byte-identical ({why})"
    elif asserts == "multires":
        wanted = set(resolutions)
        produced = sorted(Path(p).name for p in state["tiles"])
        got = {int(n.rsplit('_', 1)[1].split('m')[0]) for n in produced
               if '_' in n and 'm' in n.rsplit('_', 1)[1]}
        if got != wanted:
            results.add("mc", check_id, label, FAIL,
                        f"asked for {sorted(wanted)} m, the run produced {sorted(got)} m")
            return
        detail += f", resolutions {sorted(got)} m"
    elif asserts == "burn":
        bare_dir = solo(combo["roles"][0])
        if bare_dir is None:
            results.add("mc", check_id, label, FAIL,
                        "the bare solo run failed, so the burn was never measured")
            return
        verdict, why = _agree(bare_dir, out, plugin)
        if verdict != DIFFER:
            results.add("mc", check_id, label, FAIL,
                        f"the buildings changed nothing measurable: {why}. "
                        f"{_buildings_shape(picked['buildings']['source'])}")
            return
        detail += f"; buildings raised the surface ({why.split(' over ')[0]})"
    elif asserts == "skip_existing":
        stamps = {p: os.stat(p).st_mtime_ns for p in state["tiles"]}
        again = _mc_convert(stack, plugin, out, resolutions, timeout * 3,
                            buildings=buildings, overwrite=False, bbox=bbox)
        if again["ok"] is None:
            results.add("mc", check_id, label, FAIL,
                        f"the overwrite-off rerun failed: {_tail(again['err'] or '', 160)}")
            return
        rebuilt = [Path(p).name for p, stamp in stamps.items()
                   if os.stat(p).st_mtime_ns != stamp]
        if rebuilt:
            results.add("mc", check_id, label, FAIL,
                        f"overwrite=False still rebuilt {len(rebuilt)} existing "
                        f"tile(s): {rebuilt[:3]}")
            return
        if not any("already exist" in line for line in again["log"]):
            results.add("mc", check_id, label, FAIL,
                        "the rerun rebuilt nothing but never took the worker's "
                        "own all-tiles-exist path — the skip happened somewhere "
                        "this check does not understand")
            return
        # The PARTIAL path too: with one tile deleted, overwrite=False must
        # rebuild exactly that tile — the pre-filter alone cannot prove the
        # rebuild half works.
        victim = state["tiles"][0]
        os.remove(victim)
        third = _mc_convert(stack, plugin, out, resolutions, timeout * 3,
                            buildings=buildings, overwrite=False, bbox=bbox)
        if third["ok"] is None or not os.path.exists(victim):
            results.add("mc", check_id, label, FAIL,
                        f"with one tile deleted, overwrite=False did not rebuild "
                        f"it: {_tail(third['err'] or 'worker ok, tile missing', 140)}")
            return
        untouched = [Path(p).name for p, stamp in stamps.items()
                     if p != victim and os.stat(p).st_mtime_ns != stamp]
        if untouched:
            results.add("mc", check_id, label, FAIL,
                        f"rebuilding one missing tile also rebuilt {untouched[:3]}")
            return
        detail += (", rerun rebuilt nothing (worker's own skip path), and a "
                   "deleted tile was rebuilt alone")
    results.add("mc", check_id, label, PASS, detail)


#: Run-level cases that vary what every per-row run holds constant: the
#: sector wedge, the propagation models, the compute backends, the analysis
#: range, the cache drills and the scripted (Processing) surface — including
#: the one refusal a script never gets prompted about. Every per-row run uses
#: ITM/CPU/3 km on purpose (one canonical job, comparable across rows); these
#: cases are where the OTHER values users click first actually run.
_MATRIX_CASES = ("matrix:wedge", "matrix:model LOS",
                 "matrix:processing coverage", "matrix:processing refuses imagery",
                 "matrix:model SIMPLE_LOSS", "matrix:backend AUTO",
                 "matrix:backend GPU vs CPU", "matrix:range 10 km",
                 "matrix:cache pool-hit", "matrix:cache rebuild-flag",
                 "matrix:cache clear", "matrix:processing p2p")


def _tier_matrix_cases(manifest, results, layers, plugin, scratch_root: Path,
                       timeout: int, keep: bool) -> None:
    by_row = {r["row"]: r for r in manifest["rows"]}
    ref = by_row.get(manifest.get("reference_row"))
    ref_layer = layers.get(ref["name"]) if ref else None
    probe = manifest.get("elevation_probe") or {}
    lat, lon = float(probe.get("lat", 46.945)), float(probe.get("lon", 7.41))

    # -- wedge ------------------------------------------------------------
    check_id = "matrix:wedge"
    if ref_layer is None:
        results.add("mx", check_id, "a sector wedge confines the coverage", FAIL,
                    "the reference row is not in the project")
    else:
        scratch = scratch_root / "mx_wedge"
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            with Pipeline(scratch, keep) as pipe:
                params = _coverage_params(plugin, lat, lon, 30, 3, "wedge",
                                          az=(40.0, 140.0))
                state = _sa_analyse("mx", ref_layer, plugin,
                                    pipe.scratch / "out",
                                    [(params, "wedge 40-140")], timeout * 3)
                if not state["ok"]:
                    results.add("mx", check_id, "a sector wedge confines the coverage",
                                FAIL, _tail(" ".join((state["err"] or "").split()), 180))
                else:
                    ok, why = _wedge_confined(state["ok"][0][0], lat, lon,
                                              40.0, 140.0)
                    results.add("mx", check_id,
                                "a sector wedge confines the coverage",
                                PASS if ok else FAIL, why)
        except Exception as exc:
            results.add("mx", check_id, "a sector wedge confines the coverage",
                        FAIL, f"{type(exc).__name__}: {_shorten(str(exc), 160)}")

    # -- another model ----------------------------------------------------
    check_id = "matrix:model LOS"
    if ref_layer is None:
        results.add("mx", check_id, "a LOS run produces a sound grid", FAIL,
                    "the reference row is not in the project")
    else:
        scratch = scratch_root / "mx_los"
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            with Pipeline(scratch, keep) as pipe:
                params = _coverage_params(plugin, lat, lon, 30, 3, "los",
                                          model="LOS")
                state = _sa_analyse("mx", ref_layer, plugin,
                                    pipe.scratch / "out",
                                    [(params, "LOS")], timeout * 3)
                if not state["ok"]:
                    results.add("mx", check_id, "a LOS run produces a sound grid",
                                FAIL, _tail(" ".join((state["err"] or "").split()), 180))
                else:
                    ok, why = _sound_grid(coverage_report(Path(state["ok"][0][0])),
                                          3, 30)
                    results.add("mx", check_id, "a LOS run produces a sound grid",
                                PASS if ok else FAIL, why)
        except Exception as exc:
            results.add("mx", check_id, "a LOS run produces a sound grid",
                        FAIL, f"{type(exc).__name__}: {_shorten(str(exc), 160)}")

    # -- the scripted surface --------------------------------------------
    check_id = "matrix:processing coverage"
    if ref_layer is None:
        results.add("mx", check_id, "the Processing algorithm runs end to end",
                    FAIL, "the reference row is not in the project")
    else:
        scratch = scratch_root / "mx_proc"
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            with Pipeline(scratch, keep) as pipe:
                _run_processing_coverage(ref_layer, lat, lon,
                                         pipe.scratch / "out")
                tifs = sorted((pipe.scratch / "out").rglob("*.tif"))
                if not tifs:
                    results.add("mx", check_id,
                                "the Processing algorithm runs end to end", FAIL,
                                "processing.run returned without writing a GeoTIFF")
                else:
                    ok, why = _sound_grid(coverage_report(tifs[0]), 3, 30)
                    results.add("mx", check_id,
                                "the Processing algorithm runs end to end",
                                PASS if ok else FAIL,
                                f"{len(tifs)} GeoTIFF(s); {why}")
        except Exception as exc:
            results.add("mx", check_id, "the Processing algorithm runs end to end",
                        FAIL, f"{type(exc).__name__}: {_shorten(str(exc), 200)}")

    # -- the scripted surface must refuse imagery -------------------------
    check_id = "matrix:processing refuses imagery"
    reject = next((r for r in manifest["rows"]
                   if r.get("check") in _MUST_REJECT
                   and layers.get(r["name"]) is not None), None)
    if reject is None:
        results.add("mx", check_id, "Processing refuses imagery as a DEM", FAIL,
                    "no reject row is loaded to try")
    else:
        scratch = scratch_root / "mx_proc_reject"
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            with Pipeline(scratch, keep) as pipe:
                try:
                    _run_processing_coverage(layers[reject["name"]], lat, lon,
                                             pipe.scratch / "out")
                    results.add("mx", check_id,
                                "Processing refuses imagery as a DEM", FAIL,
                                f"the algorithm ran {reject['row']} as terrain with "
                                f"no prompt and no refusal — the scripted surface "
                                f"has no user to warn")
                except Exception as exc:
                    said = " ".join(str(exc).split())
                    ok = "imagery" in said.lower()
                    results.add("mx", check_id,
                                "Processing refuses imagery as a DEM",
                                PASS if ok else FAIL,
                                _shorten(said, 140) if ok else
                                f"it failed, but not on the classifier: "
                                f"{_shorten(said, 120)}")
        except Exception as exc:
            results.add("mx", check_id, "Processing refuses imagery as a DEM",
                        FAIL, f"{type(exc).__name__}: {_shorten(str(exc), 160)}")


def _gap_lines_suffix(cov: Dict[str, Any]) -> str:
    """Name the dropped lines, so a stripe FAIL says WHICH lines died."""
    parts = []
    if cov.get("gap_row_idx"):
        parts.append(f"rows {','.join(map(str, cov['gap_row_idx'][:8]))}")
    if cov.get("gap_col_idx"):
        parts.append(f"cols {','.join(map(str, cov['gap_col_idx'][:8]))}")
    return f" ({'; '.join(parts)})" if parts else ""


def _sound_grid(cov: Dict[str, Any], range_km: float, resolution_m: float
                ) -> Tuple[bool, str]:
    """One verdict for "this coverage grid is structurally sound"."""
    expected = 2.0 * range_km * 1000.0 / resolution_m
    problems = []
    if not cov.get("valid"):
        problems.append("no valid pixel")
    if cov.get("bands") != 1:
        problems.append(f"{cov.get('bands')} bands")
    if cov.get("gap_rows") or cov.get("gap_cols"):
        problems.append(f"{cov.get('gap_rows')}/{cov.get('gap_cols')} dropped "
                        f"interior lines{_gap_lines_suffix(cov)}")
    for axis in ("width", "height"):
        if abs(cov.get(axis, 0) - expected) > max(5.0, expected * 0.05):
            problems.append(f"{axis} {cov.get(axis)} vs ~{expected:.0f}")
    detail = (f"{cov.get('px')} px, {cov.get('valid_pct')}% valid, "
              f"{cov.get('gap_rows', '?')}/{cov.get('gap_cols', '?')} dropped "
              f"interior lines")
    return (not problems), (detail if not problems
                            else detail + " — " + "; ".join(problems))


def _wedge_confined(tif_path: str, lat: float, lon: float, az0: float,
                    az1: float, margin: float = 10.0) -> Tuple[bool, str]:
    """Are the coverage's valid pixels inside the requested sector?"""
    import numpy as np
    from osgeo import gdal
    gdal.UseExceptions()
    ds = gdal.Open(tif_path)
    band = ds.GetRasterBand(1)
    values = band.ReadAsArray().astype("float64")
    nodata = band.GetNoDataValue()
    mask = np.isfinite(values)
    if nodata is not None:
        mask &= values != nodata
    if not mask.any():
        return False, "the wedge run produced no valid pixel at all"
    gt = ds.GetGeoTransform()
    ys, xs = np.nonzero(mask)
    px_lon = gt[0] + (xs + 0.5) * gt[1]
    px_lat = gt[3] + (ys + 0.5) * gt[5]
    de = (px_lon - lon) * math.cos(math.radians(lat))
    dn = px_lat - lat
    az = (np.degrees(np.arctan2(de, dn))) % 360.0
    inside = ((az >= (az0 - margin) % 360.0) & (az <= az1 + margin)
              if az0 - margin >= 0 else
              (az >= 0) & (az <= az1 + margin) | (az >= (az0 - margin) % 360.0))
    near = (np.abs(de) < 3e-4) & (np.abs(dn) < 3e-4)   # the tx pixel itself
    stray = int((~inside & ~near).sum())
    total = int(mask.sum())
    sector_px = ((az1 - az0) % 360.0 or 360.0) / 360.0 * math.pi / 4.0 \
        * mask.shape[0] * mask.shape[1]
    if total < 0.2 * sector_px:
        return False, (f"only {total:,} valid px where the {az0:.0f}–{az1:.0f}° "
                       f"sector holds ~{sector_px:,.0f} — a nearly-empty wedge "
                       f"confines nothing")
    ok = stray <= max(5, total // 100)
    return ok, (f"{total:,} valid px, {stray:,} outside {az0:.0f}–{az1:.0f}° "
                f"(±{margin:.0f}°)")


#: The registered AetherProvider, pinned for the process lifetime.
#: `QgsProcessingRegistry.addProvider` is a sip /Transfer/: it parents the
#: Python provider to the registry's *Python wrapper*, a throwaway local.
#: Without this reference the wrapper is collected between calls, the Python
#: subclass (and its algorithms) die while the C++ shells survive, and the
#: next `processing.run` fails with "Error creating algorithm from
#: createInstance()". The shipped plugin is immune — plugin.py keeps
#: `self.provider` alive — so this is runner bootstrap, not product.
_PROCESSING_PROVIDER = None


def _ensure_waveshed_provider():
    """QGIS Processing initialised and the waveshed provider registered."""
    global _PROCESSING_PROVIDER
    from qgis.core import QgsApplication
    # Headless, QGIS's own `processing` plugin is not on sys.path (inside the
    # GUI the plugin manager puts it there). Same framework, same path QGIS
    # ships it at — nothing is stubbed.
    plugins_dir = os.path.join(QgsApplication.pkgDataPath(), "python", "plugins")
    if plugins_dir not in sys.path and os.path.isdir(plugins_dir):
        sys.path.append(plugins_dir)
    from qgis import processing as qgis_processing
    from processing.core.Processing import Processing
    Processing.initialize()
    registry = QgsApplication.processingRegistry()
    from waveshed.provider import AetherProvider
    existing = registry.providerById("waveshed")
    if not isinstance(existing, AetherProvider):
        # `is None` cannot see a zombie: a dead Python provider still
        # answers as a bare QgsProcessingProvider wrapper.
        if existing is not None:
            registry.removeProvider("waveshed")
        _PROCESSING_PROVIDER = AetherProvider()
        registry.addProvider(_PROCESSING_PROVIDER)
    return qgis_processing


def _run_processing_coverage(layer, lat: float, lon: float, out_dir: Path):
    """The scripted surface, exactly as a model or batch job drives it."""
    qgis_processing = _ensure_waveshed_provider()
    out_dir.mkdir(parents=True, exist_ok=True)
    return qgis_processing.run("waveshed:coverage", {
        "INPUT_DEM": layer, "TX_LAT": float(lat), "TX_LON": float(lon),
        "TX_HEIGHT": 30.0, "FREQ_MHZ": 900.0, "ERP_WATTS": 10.0,
        "MODEL": 1,          # ITM
        "RESOLUTION": 3,     # index into VALID_RESOLUTIONS -> 30 m
        "MAX_RANGE": 3, "BACKEND": 2,   # CPU
        "OUTPUT_DIR": str(out_dir),
    })


def _run_processing_p2p(layer, tx_lat: float, tx_lon: float, rx_lat: float,
                        rx_lon: float, out_dir: Path):
    """The second scripted surface — checklist 8.9's other half."""
    qgis_processing = _ensure_waveshed_provider()
    out_dir.mkdir(parents=True, exist_ok=True)
    return qgis_processing.run("waveshed:p2p", {
        "INPUT_DEM": layer,
        "TX_LAT": float(tx_lat), "TX_LON": float(tx_lon), "TX_HEIGHT": 30.0,
        "RX_LAT": float(rx_lat), "RX_LON": float(rx_lon), "RX_HEIGHT": 2.0,
        "MODEL": 1,          # ITM
        "RESOLUTION": 3,     # index into VALID_RESOLUTIONS -> 30 m
        "BACKEND": 2,        # CPU
        "OUTPUT_DIR": str(out_dir),
    })


def _tier_matrix_extra(manifest, results: Results, layers, plugin,
                       scratch_root: Path, timeout: int, keep: bool) -> None:
    """The matrix cases beyond the original four: the OTHER models, both
    backends, a multi-tile range, the cache drills and the scripted P2P.

    Every per-row pipeline holds ITM/CPU/3 km constant so rows stay
    comparable; a user's first click uses the defaults (AUTO backend) and
    their second changes exactly these knobs — which is why each knob gets
    one real end-to-end run here.
    """
    import numpy as np
    from osgeo import gdal
    gdal.UseExceptions()
    adapter = plugin["adapter"]
    by_row = {r["row"]: r for r in manifest["rows"]}
    ref = by_row.get(manifest.get("reference_row"))
    ref_layer = layers.get(ref["name"]) if ref else None
    probe = manifest.get("elevation_probe") or {}
    lat, lon = float(probe.get("lat", 46.945)), float(probe.get("lon", 7.41))

    def sa_case(check_id: str, label: str, run) -> None:
        """One SA-shaped case: fresh Pipeline, *run(pipe)* -> (ok, detail)."""
        if ref_layer is None:
            results.add("mx", check_id, label, FAIL,
                        "the reference row is not in the project")
            return
        scratch = scratch_root / re.sub(r"[^a-z0-9]+", "_", check_id.lower())
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            with Pipeline(scratch, keep) as pipe:
                ok, why = run(pipe)
                results.add("mx", check_id, label, PASS if ok else FAIL, why)
        except Exception as exc:
            results.add("mx", check_id, label, FAIL,
                        f"{type(exc).__name__}: {_shorten(str(exc), 160)}")

    #: The view directories the LAST ``one_sa`` run's prepare_terrain
    #: returned — the plugin's own record, not a recomputed guess.
    last_views: Dict[str, Any] = {}

    def one_sa(pipe, name, **overrides):
        base = dict(resolution=30, range_km=3, model="ITM")
        base.update({k: v for k, v in overrides.items()
                     if k in ("resolution", "range_km", "model")})
        params = _coverage_params(plugin, lat, lon, base["resolution"],
                                  base["range_km"], name, model=base["model"])
        if "backend" in overrides:
            import dataclasses as _dc
            params = _dc.replace(params, backend=overrides["backend"])
        _pop_view_dirs(plugin)      # only THIS run's view may be read back
        state = _sa_analyse("mx", ref_layer, plugin, pipe.scratch / name,
                            [(params, name)], timeout * 3)
        last_views["v"] = _pop_view_dirs(plugin)
        return params, state

    # -- the third model --------------------------------------------------
    def run_simple_loss(pipe):
        _params, state = one_sa(pipe, "simple_loss", model="SIMPLE_LOSS")
        if not state["ok"]:
            return False, _tail(" ".join((state["err"] or "").split()), 180)
        return _sound_grid(coverage_report(Path(state["ok"][0][0])), 3, 30)
    sa_case("matrix:model SIMPLE_LOSS", "a SIMPLE_LOSS run produces a sound grid",
            run_simple_loss)

    # -- the backend a first click actually uses --------------------------
    def run_auto(pipe):
        _params, state = one_sa(pipe, "auto", backend="AUTO")
        if not state["ok"]:
            return False, _tail(" ".join((state["err"] or "").split()), 180)
        ok, why = _sound_grid(coverage_report(Path(state["ok"][0][0])), 3, 30)
        return ok, f"backend AUTO (the GUI default): {why}"
    sa_case("matrix:backend AUTO", "the AUTO backend produces a sound grid",
            run_auto)

    # -- GPU against CPU, same job, same terrain --------------------------
    def run_gpu_vs_cpu(pipe):
        _p, cpu = one_sa(pipe, "cpu", backend="CPU")
        if not cpu["ok"]:
            return False, ("the CPU half failed: "
                           + _tail(" ".join((cpu["err"] or "").split()), 150))
        _p, gpu = one_sa(pipe, "gpu", backend="GPU")
        if not gpu["ok"]:
            return False, ("the GPU half failed — a machine that cannot run "
                           "the GPU backend cannot green this suite: "
                           + _tail(" ".join((gpu["err"] or "").split()), 150))
        a = gdal.Open(cpu["ok"][0][0])
        b = gdal.Open(gpu["ok"][0][0])
        va = a.GetRasterBand(1).ReadAsArray().astype("float64")
        vb = b.GetRasterBand(1).ReadAsArray().astype("float64")
        if va.shape != vb.shape:
            return False, f"different grids: {va.shape} vs {vb.shape}"
        na, nb = a.GetRasterBand(1).GetNoDataValue(), b.GetRasterBand(1).GetNoDataValue()
        mask = np.isfinite(va) & np.isfinite(vb)
        if na is not None:
            mask &= va != na
        if nb is not None:
            mask &= vb != nb
        if not mask.any():
            return False, "the two backends share no valid pixel"
        diff = np.abs(va[mask] - vb[mask])
        p95 = float(np.percentile(diff, 95))
        worst = float(diff.max())
        agree = int((np.abs(va[mask] - vb[mask]) <= 0.5).sum())
        # The allowance pins the engine's SHIPPED parity level, not an
        # aspiration: the GPU/CPU ITM backends measurably sit at p95 ~4 dB
        # on this scenario (AETHER tests/bench/improvement_log.md — four dh
        # mitigation rounds rejected; the residual is intrinsic to the
        # stride-2 decimation design). 5 dB catches what a check can
        # honestly catch today: stale shaders, row shear, broken banding,
        # a backend drifting from its documented envelope.
        ok = p95 <= 5.0
        return ok, (f"GPU vs CPU over {int(mask.sum()):,} shared px: p95 "
                    f"|difference| {p95:.2f} dB, max {worst:.2f} dB, "
                    f"{agree:,} within 0.5 dB"
                    + ("" if ok else " — the backends disagree beyond the "
                                    "shipped parity envelope (p95 allowance "
                                    "5 dB; see AETHER improvement_log.md)"))
    sa_case("matrix:backend GPU vs CPU",
            "GPU and CPU compute the same coverage", run_gpu_vs_cpu)

    # -- a range that spans multiple terrain tiles ------------------------
    def run_range10(pipe):
        params, state = one_sa(pipe, "range10", range_km=10)
        if not state["ok"]:
            return False, _tail(" ".join((state["err"] or "").split()), 180)
        mc = plugin["mc"]
        bbox = adapter.analysis_bbox(lat, lon, 10)
        expected = {t["filename"] for _r, t in
                    mc._enumerate_tiles(mc._snap_bbox(bbox, [30]), [30])}
        view, caveat = _sa_view_from_run(last_views.get("v"),
                                         ref_layer.source(), lat, lon, 10.0,
                                         30, plugin)
        got = {Path(p).name for p in plugin["abt"].list_tiles(view)} \
            if view and os.path.isdir(view) else set()
        if got != expected:
            return False, (f"the 10 km terrain view holds {sorted(got)[:3]} "
                           f"vs the enumeration's {sorted(expected)[:3]} — "
                           f"a dropped tile at a seam"
                           + (f" ({caveat})" if caveat else ""))
        ok, why = _sound_grid(coverage_report(Path(state["ok"][0][0])), 10, 30)
        return ok, f"{len(expected)} terrain tile(s) under one disk; {why}"
    sa_case("matrix:range 10 km",
            "a 10 km run spans multiple tiles without seams", run_range10)

    # -- the cache drills, against the offline local fixture --------------
    local = next((r for r in manifest["rows"]
                  if r.get("check") == "both"
                  and ("127.0.0.1" in (r.get("source") or "")
                       or "localhost" in (r.get("source") or ""))), None)
    local_layer = layers.get(local["name"]) if local else None
    if local is None or local_layer is None:
        for check_id in ("matrix:cache pool-hit", "matrix:cache rebuild-flag",
                         "matrix:cache clear"):
            results.add("mx", check_id, "cache drill", FAIL,
                        "no offline local-fixture row is loaded to drill against")
    else:
        scratch = scratch_root / "mx_cache"
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            with Pipeline(scratch, keep) as pipe:
                _cache_drills(local, local_layer, plugin, pipe, manifest,
                              results, timeout)
        except Exception as exc:
            for check_id in ("matrix:cache pool-hit", "matrix:cache rebuild-flag",
                             "matrix:cache clear"):
                if ("mx", check_id) not in results.executed:
                    results.add("mx", check_id, "cache drill", FAIL,
                                f"{type(exc).__name__}: {_shorten(str(exc), 140)}")

    # -- the scripted P2P surface -----------------------------------------
    check_id = "matrix:processing p2p"
    if ref_layer is None:
        results.add("mx", check_id, "the P2P algorithm runs end to end", FAIL,
                    "the reference row is not in the project")
    else:
        scratch = scratch_root / "mx_proc_p2p"
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            with Pipeline(scratch, keep) as pipe:
                _run_processing_p2p(ref_layer, lat, lon, 46.9200, 7.4370,
                                    pipe.scratch / "out")
                csvs = sorted((pipe.scratch / "out").rglob("*.csv"))
                if not csvs:
                    results.add("mx", check_id,
                                "the P2P algorithm runs end to end", FAIL,
                                "processing.run returned without writing a "
                                "result CSV")
                else:
                    excess, detail = _excess_loss_db(
                        str(csvs[0]), (lat, lon), (46.9200, 7.4370),
                        freq_mhz=433.0)
                    ok = excess is not None
                    results.add("mx", check_id,
                                "the P2P algorithm runs end to end",
                                PASS if ok else FAIL,
                                detail if ok else
                                f"the result CSV is not the contract's: {detail}")
        except Exception as exc:
            results.add("mx", check_id, "the P2P algorithm runs end to end",
                        FAIL, f"{type(exc).__name__}: {_shorten(str(exc), 200)}")


def _cache_drills(row, layer, plugin, pipe: "Pipeline", manifest,
                  results: Results, timeout: int) -> None:
    """Pool hit, targeted `.rebuild` refetch, Clear cache — offline, real.

    Three runs of the same job against the local fixture: the second must
    re-download nothing (checklist 9.1), a `.rebuild` flag must refetch
    exactly its tile (9.2), and the plugin's own Clear cache must leave
    nothing behind (0.4).
    """
    adapter = plugin["adapter"]
    resolution = int(row.get("pipeline_res_m") or 30)
    range_km = int(row.get("pipeline_range_km") or 3)
    lat, lon = _probe_for(row, manifest)

    def run_once(name):
        params = _coverage_params(plugin, lat, lon, resolution, range_km, name)
        tap = _MessageLogTap()
        with tap:
            state = _sa_analyse("mx", layer, plugin, pipe.scratch / name,
                                [(params, name)], timeout * 3)
        return state, tap

    def pool_tiles():
        root = Path(adapter.get_cache_dir()) / adapter._POOL_DIRNAME
        return {str(p): os.stat(p).st_mtime_ns
                for p in sorted(root.rglob("*.abt"))}

    first, _tap = run_once("seed")
    before = pool_tiles()
    if not first["ok"] or not before:
        results.add("mx", "matrix:cache pool-hit",
                    "a second identical run is a pool hit", FAIL,
                    "the seeding run failed or pooled nothing: "
                    + _tail(" ".join((first["err"] or "").split()), 150))
    else:
        second, tap = run_once("hit")
        after = pool_tiles()
        touched = [Path(p).name for p, stamp in before.items()
                   if after.get(p) != stamp]
        hit_logged = "HIT" in tap.text()
        ok = bool(second["ok"]) and not touched and hit_logged
        results.add("mx", "matrix:cache pool-hit",
                    "a second identical run is a pool hit",
                    PASS if ok else FAIL,
                    f"{len(before)} pool tile(s) untouched, HIT logged" if ok else
                    ("; ".join(filter(None, [
                        None if second["ok"] else "the second run failed",
                        f"re-downloaded {touched[:3]}" if touched else None,
                        None if hit_logged else "no HIT line in the terrain log"]))))

    # -- targeted rebuild --------------------------------------------------
    tiles = pool_tiles()
    if not tiles:
        results.add("mx", "matrix:cache rebuild-flag",
                    "a .rebuild flag refetches exactly its tile", FAIL,
                    "no pool tile to flag")
    else:
        victim = sorted(tiles)[0]
        open(victim + adapter._REBUILD_SUFFIX, "w").close()
        third, _tap = run_once("rebuild")
        after = pool_tiles()
        rebuilt = [Path(p).name for p, stamp in tiles.items()
                   if after.get(p) != stamp]
        flag_cleared = not os.path.exists(victim + adapter._REBUILD_SUFFIX)
        ok = (bool(third["ok"]) and rebuilt == [Path(victim).name]
              and flag_cleared)
        results.add("mx", "matrix:cache rebuild-flag",
                    "a .rebuild flag refetches exactly its tile",
                    PASS if ok else FAIL,
                    f"{Path(victim).name} refetched alone, flag cleared" if ok else
                    ("; ".join(filter(None, [
                        None if third["ok"] else "the run failed",
                        f"rebuilt {rebuilt[:3]} (wanted exactly "
                        f"[{Path(victim).name}])"
                        if rebuilt != [Path(victim).name] else None,
                        None if flag_cleared else "the flag is still set"]))))

    # -- the plugin's own Clear cache --------------------------------------
    adapter.clear_cache()
    left = [str(p) for p in Path(pipe.cache).rglob("*.abt")]
    results.add("mx", "matrix:cache clear",
                "Clear cache leaves no tile behind",
                PASS if not left else FAIL,
                "pool and views empty" if not left else
                f"{len(left)} .abt file(s) survived (first: {Path(left[0]).name})")


def _tier_folder_cases(manifest, results: Results, plugin,
                       scratch_root: Path, timeout: int, keep: bool) -> None:
    """The Map Converter's FOLDER input, case by case, from the manifest.

    Checklist Phase 3, automated: each case builds the exact `_LayerEntry`
    the GUI's Add Folder handler builds (same scanner, same CRS detection,
    same fallbacks) and drives the shipped resolve + worker. Good folders
    prove their ground by probe; broken ones must refuse naming the file
    and the cause the catalogue quotes.
    """
    adapter = plugin["adapter"]
    mc = plugin["mc"]
    out_dir = Path(manifest["__manifest_dir__"])
    probe = manifest.get("elevation_probe") or {}

    for case in manifest.get("folder_cases") or []:
        cid = case.get("id", "?")
        check_id = f"folder:{cid}"
        label = f"folder input: {cid}"
        scratch = scratch_root / ("folder_" + re.sub(r"[^a-z0-9]+", "_", cid.lower()))
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            with Pipeline(scratch, keep) as pipe:
                _run_folder_case(case, cid, check_id, label, plugin, adapter,
                                 mc, out_dir, probe, pipe, results, timeout)
        except Exception as exc:
            results.add("folder", check_id, label, FAIL,
                        f"the check itself raised {type(exc).__name__}: "
                        f"{_shorten(str(exc), 160)}")


def _run_folder_case(case, cid, check_id, label, plugin, adapter, mc, out_dir,
                     probe, pipe: "Pipeline", results: Results,
                     timeout: int) -> None:
    if case.get("path") is None:
        folder = str(pipe.scratch / "empty_folder")
        os.makedirs(folder, exist_ok=True)
    else:
        folder = str(out_dir / case["path"])
    if not os.path.isdir(folder):
        results.add("folder", check_id, label, FAIL,
                    f"the fixture folder is missing: {folder} — run "
                    f"--stages fabricate")
        return

    if case.get("scanner_excludes"):
        listed = {os.path.basename(p)
                  for p in adapter.list_terrain_files(folder)}
        if case["scanner_excludes"] in listed:
            results.add("folder", check_id, label, FAIL,
                        f"the shared scanner listed {case['scanner_excludes']} "
                        f"— a {Path(case['scanner_excludes']).suffix} file in "
                        f"a terrain folder must be ignored")
            return

    # The entry the GUI's Add Folder builds, via the tab's own helpers.
    try:
        crs_id = mc._detect_folder_crs(folder)
    except Exception:  # noqa: BLE001 — a broken fixture must reach the worker
        crs_id = None
    crs_id = crs_id or case.get("ask_crs") or ""
    try:
        native = mc._detect_folder_resolution(folder)
    except Exception:  # noqa: BLE001
        native = None
    resolution = int(case.get("res_m") or 30)
    range_km = int(case.get("range_km") or 3)
    if case.get("center"):
        lat, lon = float(case["center"][0]), float(case["center"][1])
    else:
        lat = float(probe.get("lat", 46.945))
        lon = float(probe.get("lon", 7.41))
    bbox = adapter.analysis_bbox(lat, lon, range_km)
    entry = mc._LayerEntry(layer_type="raster", source_path=folder,
                           crs_authid=crs_id, native_res_m=native,
                           target_resolutions=[resolution], extent=dict(bbox))
    try:
        state = _mc_run_entries([entry], plugin, pipe.scratch / "out",
                                [resolution], timeout * 3)
    except RuntimeError as exc:
        state = {"ok": None, "err": str(exc), "log": [], "tiles": []}
    said = _worker_complaint(state)

    if case.get("expect") == "error":
        if state["ok"] is not None:
            results.add("folder", check_id, label, FAIL,
                        f"it converted {len(state['tiles'])} tile(s) from a "
                        f"folder the catalogue says must be refused "
                        f"({case.get('note', '')})")
            return
        if _is_synthesized(said):
            results.add("folder", check_id, label, FAIL,
                        f"the runner's own timeout is not a refusal: "
                        f"{_tail(said, 110)}")
            return
        low = said.lower()
        missing = [m for m in (case.get("must_name") or [])
                   if str(m).lower() not in low]
        results.add("folder", check_id, label,
                    FAIL if missing else PASS,
                    (f"refused without naming {missing}: {_tail(said, 100)}"
                     if missing else _tail(said, 130)))
        return

    if state["ok"] is None:
        results.add("folder", check_id, label, FAIL, _tail(said, 220))
        return
    report = abt_report(state["tiles"], plugin)
    if "unreadable" in report or not report.get("real_px"):
        results.add("folder", check_id, label, FAIL,
                    "the run produced tiles with no real sample")
        return
    problems = []
    band = case.get("elev_m") or []
    for probe_pt in case.get("probes") or []:
        at = abt_at(report["grids"], float(probe_pt[0]), float(probe_pt[1]),
                    plugin)
        if at is None or at <= plugin["abt"].MIN_VALID_ELEV_M:
            problems.append(f"probe {probe_pt} is "
                            f"{'outside the tiles' if at is None else 'VOID'}")
        elif len(band) == 2 and not (float(band[0]) <= at <= float(band[1])):
            problems.append(f"{at:,.1f} m at {probe_pt}, outside "
                            f"{band[0]:,.0f}..{band[1]:,.0f} m")

    detail = (f"{report['real_px']:,}/{report['total_px']:,} real, "
              f"{report.get('min_m', 0):,.1f}..{report.get('max_m', 0):,.1f} m")
    if case.get("winner"):
        # The overlap contract, by measurement: a second folder holding ONLY
        # the winner must produce the same probe elevation.
        solo_dir = pipe.scratch / "winner_only"
        solo_dir.mkdir(parents=True, exist_ok=True)
        source_file = os.path.join(folder, case["winner"])
        shutil.copy2(source_file, solo_dir / case["winner"])
        solo_entry = mc._LayerEntry(layer_type="raster",
                                    source_path=str(solo_dir),
                                    crs_authid=crs_id, native_res_m=native,
                                    target_resolutions=[resolution],
                                    extent=dict(bbox))
        solo = _mc_run_entries([solo_entry], plugin, pipe.scratch / "solo_out",
                               [resolution], timeout * 3)
        if solo["ok"] is None:
            problems.append("the winner-only reference run failed, so "
                            "priority was never measured")
        else:
            solo_report = abt_report(solo["tiles"], plugin)
            pt = (case.get("probes") or [[probe.get("lat", 46.945),
                                          probe.get("lon", 7.41)]])[0]
            folder_at = abt_at(report["grids"], float(pt[0]), float(pt[1]),
                               plugin)
            winner_at = abt_at(solo_report.get("grids", []), float(pt[0]),
                               float(pt[1]), plugin)
            delta = float(case.get("loser_delta_m") or 50.0)
            if folder_at is None or winner_at is None:
                problems.append("the priority probe fell outside a tile")
            elif abs(folder_at - winner_at) > 0.5:
                problems.append(
                    f"the folder reads {folder_at:,.1f} m where "
                    f"{case['winner']} alone reads {winner_at:,.1f} m — "
                    + (f"the LOSER won (delta ~{delta:,.0f} m)"
                       if abs(abs(folder_at - winner_at) - delta) <= 2.0
                       else "priority is broken"))
            else:
                detail += f"; {case['winner']} won at the probe"
    results.add("folder", check_id, label, FAIL if problems else PASS,
                ("; ".join(problems) if problems else detail))


def _tier_res_sweep(manifest, results: Results, layers, plugin,
                    scratch_root: Path, timeout: int, keep: bool) -> None:
    """One REAL conversion per catalogue resolution — checklist Phase 7.

    Every per-row pipeline runs at one canonical resolution; this is where
    2/5/10/30/90/250 m each produce actual tiles, checked the cheapest,
    hardest way there is: tile names against the plugin's own enumeration
    and file sizes byte-exact against the manifest's ladder.
    """
    picked = _pick_roles(manifest)
    dem_row = picked.get("dem")
    dem_layer = layers.get(dem_row["name"]) if dem_row else None
    probe = manifest.get("elevation_probe") or {}
    lat, lon = float(probe.get("lat", 46.945)), float(probe.get("lon", 7.41))
    adapter = plugin["adapter"]
    mc = plugin["mc"]

    for res in manifest.get("resolutions") or []:
        res = int(res)
        check_id = f"sweep:{res}m"
        label = f"a real conversion at {res} m"
        if dem_row is None or dem_layer is None:
            results.add("sweep", check_id, label, FAIL,
                        "the catalogue has no loadable full-coverage local DEM")
            continue
        scratch = scratch_root / f"sweep_{res}m"
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            with Pipeline(scratch, keep) as pipe:
                bbox = adapter.analysis_bbox(lat, lon, 1)
                state = _mc_convert([(dem_row, dem_layer)], plugin,
                                    pipe.scratch / "out", [res], timeout * 3,
                                    bbox=bbox)
                if state["ok"] is None:
                    results.add("sweep", check_id, label, FAIL,
                                _tail(_worker_complaint(state), 200))
                    continue
                expected = {t["filename"] for _r, t in mc._enumerate_tiles(
                    mc._snap_bbox(bbox, [res]), [res])}
                got = {Path(t).name for t in state["tiles"]}
                if got != expected:
                    results.add("sweep", check_id, label, FAIL,
                                f"produced {sorted(got)[:3]} vs the plugin's "
                                f"own enumeration {sorted(expected)[:3]}")
                    continue
                wanted_bytes = (manifest.get("tile_bytes") or {}).get(str(res))
                wrong = [Path(p).name for p in state["tiles"]
                         if wanted_bytes and
                         os.path.getsize(p) != int(wanted_bytes)]
                if not wanted_bytes:
                    results.add("sweep", check_id, label, FAIL,
                                f"the catalogue's tile_bytes table has no "
                                f"entry for {res} m")
                    continue
                if wrong:
                    results.add("sweep", check_id, label, FAIL,
                                f"{wrong[:3]} are not exactly "
                                f"{int(wanted_bytes):,} B")
                    continue
                report = abt_report(state["tiles"], plugin)
                at = abt_at(report.get("grids", []), lat, lon, plugin)
                problems = []
                if not report.get("real_px"):
                    problems.append("every sample is VOID")
                elif report["min_m"] == report["max_m"]:
                    problems.append(f"every real sample is "
                                    f"{report['min_m']:,.1f} m — constant")
                if at is None or at <= plugin["abt"].MIN_VALID_ELEV_M:
                    problems.append("the probe pixel is void or outside")
                results.add("sweep", check_id, label,
                            FAIL if problems else PASS,
                            "; ".join(problems) if problems else
                            f"{len(got)} tile(s), byte-exact at "
                            f"{int(wanted_bytes):,} B, "
                            f"{report['min_m']:,.1f}..{report['max_m']:,.1f} m, "
                            f"{at:,.1f} m at the probe")
        except Exception as exc:
            results.add("sweep", check_id, label, FAIL,
                        f"the check itself raised {type(exc).__name__}: "
                        f"{_shorten(str(exc), 160)}")


def _build_reference(manifest, results: Results, layers, plugin,
                     ref_root: Path, timeout: int,
                     keep: bool) -> Optional["_Reference"]:
    """The catalogue's reference terrain, built ONCE, kept for the whole tier.

    Its own worker run, its own cache, its own output directory — nothing a
    row later acquires can touch these tiles, which is the property the old
    per-row re-acquisition destroyed (the reference overwrote, via hard
    links, the very tiles it was about to be compared with).

    *ref_root* is deliberately NOT ``scratch_root``: it has to OUTLIVE every
    row, and ``scratch_root`` is the tree every per-row and per-case
    ``Pipeline`` carves its own scratch out of and ``rmtree``s on the way
    out. Living next to it instead of inside it puts the reference out of
    reach of every teardown but the tier's own final one.
    """
    by_row = {r["row"]: r for r in manifest["rows"]}
    ref = by_row.get(manifest.get("reference_row"))
    layer = layers.get(ref["name"]) if ref else None
    results.plan("-", "reference.terrain")
    if ref is None or layer is None:
        results.add("-", "reference.terrain", "the reference terrain is built once",
                    FAIL, "the catalogue's reference row is not in the project")
        return None
    probe = manifest.get("elevation_probe") or {}
    lat, lon = float(probe.get("lat", 46.945)), float(probe.get("lon", 7.41))
    adapter = plugin["adapter"]
    ref_dir = ref_root / "reference_terrain"
    scratch = ref_root / "reference_scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        with Pipeline(scratch, keep) as pipe:
            state = _mc_convert([(ref, layer)], plugin, ref_dir, [30],
                                timeout * 3,
                                bbox=adapter.analysis_bbox(lat, lon, 3))
    except RuntimeError as exc:
        state = {"ok": None, "err": str(exc), "tiles": []}
    if state["ok"] is None or not state["tiles"]:
        why = _worker_complaint(state) or \
            "the worker reported success but wrote no tile"
        results.add("-", "reference.terrain", "the reference terrain is built once",
                    FAIL, f"the reference run failed — every agreement check "
                          f"below fails with it: {_tail(why, 160)}")
        return None
    report = abt_report(state["tiles"], plugin)
    reference = _Reference(ref_dir, state["tiles"])
    missing = _reference_missing(reference, plugin)
    if missing:
        results.add("-", "reference.terrain", "the reference terrain is built once",
                    FAIL, f"the worker reported success but "
                          f"{len(missing)} of {len(reference.names)} tile(s) "
                          f"are already missing or unreadable under {ref_dir} "
                          f"(first: {missing[0]})")
        return None
    results.add("-", "reference.terrain", "the reference terrain is built once",
                PASS, f"{len(state['tiles'])} tile(s), "
                      f"{report.get('min_m', 0):,.1f}..{report.get('max_m', 0):,.1f} m, "
                      f"kept at {ref_dir} — outside the per-row scratch tree — "
                      f"for every row's agreement check")
    return reference


def tier_pipeline(manifest: Dict[str, Any], results: Results, only: Sequence[str],
                  timeout: int, keep: bool) -> None:
    """Run the WHOLE pipeline for every catalogue row, one clean cache each.

    This is the tier that answers the only question that matters: put this
    layer in, click Run, and does the thing the USER would get come out
    right? Every run below goes through the shipped workers; every check
    below was declared before the first one started, so anything that does
    not run is a NOTRUN — and a NOTRUN fails the run.
    """
    try:
        plugin = _plugin_modules()
    except Exception as exc:
        results.plan("-", "plugin.pairing")
        results.add("-", "plugin.pairing", "the plugin under test is this repo's",
                    FAIL, f"the plugin could not even be imported: "
                          f"{type(exc).__name__}: {_shorten(str(exc), 140)}")
        return
    if not _check_plugin_pairing(plugin, results):
        return
    project = None

    # Declare EVERYTHING first. A crash after this point leaves NOTRUNs, not
    # silence.
    rows = [r for r in manifest["rows"] if row_selected(r["row"], only)]
    results.plan("-", "engine.present")
    results.plan("-", "void.sentinel")
    results.plan("-", "project.open")
    results.plan("-", "reference.terrain")
    for row in rows:
        _plan_pipeline_row(row, manifest, results, plugin)
    # The case tiers — stacks, matrix, folder inputs, the resolution sweep —
    # are not row-addressable, so a --rows debug run skips them entirely
    # (planned nothing, ran nothing, said so; the report is PARTIAL anyway)
    # instead of dragging an hour of unrelated cases into every repro.
    run_cases = not only
    if run_cases:
        for combo in _COMBINATIONS:
            results.plan("mc", f"mc:{combo['name']}")
        for case in _MATRIX_CASES:
            results.plan("mx", case)
        for case in manifest.get("folder_cases") or []:
            results.plan("folder", f"folder:{case.get('id', '?')}")
        for res in manifest.get("resolutions") or []:
            results.plan("sweep", f"sweep:{int(res)}m")
    else:
        results.note(f"--rows {','.join(only)} selects catalogue rows only — "
                     f"the case tiers (stacks, matrix, folder, sweep) are "
                     f"skipped; run without --rows for the full set")

    binaries = {}
    already = ("-", "engine.present") in results.executed
    for name in ("aether_converter", "aether_core", "aether_export"):
        found = find_binary(name)
        if found is None:
            if not already:
                results.add("-", "engine.present", "engine binaries", FAIL,
                            f"{name} not found (set AETHER_BIN_DIR) — every pipeline "
                            f"check below is NOTRUN, and a NOTRUN fails the run")
            return
        binaries[name] = found
    if not already:
        results.add("-", "engine.present", "engine binaries", PASS,
                    ", ".join(sorted(p.name for p in binaries.values())))

    _check_void_sentinel(plugin, results)
    project = open_project(manifest, results)
    if project is None:
        return
    layers = {l.name(): l for l in project.mapLayers().values()}
    out_dir = Path(manifest["__manifest_dir__"])
    scratch_root = out_dir / ".pipeline"
    # The reference terrain lives NEXT TO the scratch tree, not inside it.
    # Every row and every case carves its Pipeline scratch out of
    # scratch_root and rmtree's it on the way out; the reference has to
    # survive all of them, because every row is compared against it. Still
    # inside the run's own temp area (removed with it below), never in the
    # fixture data.
    ref_root = out_dir / ".pipeline_reference"
    shutil.rmtree(scratch_root, ignore_errors=True)
    shutil.rmtree(ref_root, ignore_errors=True)
    scratch_root.mkdir(parents=True, exist_ok=True)
    ref_root.mkdir(parents=True, exist_ok=True)

    try:
        reference = _build_reference(manifest, results, layers, plugin,
                                     ref_root, timeout, keep)
        for row in rows:
            scratch = scratch_root / row["row"].replace(".", "_")
            scratch.mkdir(parents=True, exist_ok=True)
            try:
                with Pipeline(scratch, keep) as pipe:
                    _pipeline_row(row, layers.get(row["name"]), plugin, pipe,
                                  manifest, results, timeout, reference)
            except Exception as exc:
                results.add(row["row"], "pipeline.crashed",
                            "the row's pipeline ran to completion", FAIL,
                            f"the check itself raised {type(exc).__name__}: {exc}")
                # A crash mid-row owes the same debt an early return does:
                # the checks it never reached are FAILs with a reason, not
                # NOTRUN holes. The boundary scenarios below still run.
                _fail_rest(row["row"], row, results,
                           f"the row's pipeline raised {type(exc).__name__}")
            # The boundary scenarios, each in its OWN cache: an overrun that
            # shared the row's pool would report the canonical run's tiles as
            # its own, and vice versa.
            for scenario in row.get("overrun") or []:
                sid = str(scenario.get("id", "?"))
                layer = layers.get(row["name"])
                # This scenario's OWN checks — .warned/.sea/.mc_void/.cov are
                # planned alongside .run, so filing only .run and moving on
                # left four NOTRUNs per scenario.
                mine = _overrun_checks_of(sid)
                if layer is None:
                    results.add(row["row"], f"overrun:{sid}.run",
                                "the over-the-box run completes", FAIL,
                                "the layer is not in the project")
                    _fail_rest(row["row"], row, results,
                               "the layer is not in the project", only=mine)
                    continue
                ovr_scratch = scratch_root / (
                    row["row"].replace(".", "_") + "_ovr_" +
                    re.sub(r"[^a-z0-9]+", "_", sid.lower()))
                ovr_scratch.mkdir(parents=True, exist_ok=True)
                try:
                    with Pipeline(ovr_scratch, keep) as pipe:
                        _pipeline_overrun_scenario(row, scenario, layer, plugin,
                                                   pipe, manifest, results,
                                                   timeout)
                except Exception as exc:
                    results.add(row["row"], f"overrun:{sid}.run",
                                "the over-the-box run completes", FAIL,
                                f"the check itself raised "
                                f"{type(exc).__name__}: {exc}")
                    _fail_rest(row["row"], row, results,
                               f"the scenario raised {type(exc).__name__}",
                               only=mine)
                else:
                    _fail_rest(row["row"], row, results,
                               "the scenario driver returned without filing it",
                               only=mine)
        if run_cases:
            _tier_pipeline_combinations(manifest, results, layers, plugin,
                                        scratch_root, timeout, keep)
            _tier_matrix_cases(manifest, results, layers, plugin, scratch_root,
                               timeout, keep)
            _tier_matrix_extra(manifest, results, layers, plugin, scratch_root,
                               timeout, keep)
            _tier_folder_cases(manifest, results, plugin, scratch_root,
                               timeout, keep)
            _tier_res_sweep(manifest, results, layers, plugin, scratch_root,
                            timeout, keep)
    finally:
        if keep:
            results.note(f"pipeline scratch kept at {scratch_root}")
            results.note(f"reference terrain kept at {ref_root}")
        else:
            shutil.rmtree(scratch_root, ignore_errors=True)
            # Last, and only here: the reference outlives every row by design.
            shutil.rmtree(ref_root, ignore_errors=True)


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
    elif payload["skipped"] or payload["not_run"] or unbuilt:
        lines.append("Result: **FAILED** — checks the catalogue declares did not run "
                     "(skipped, not run, or their row was never built), and a check "
                     "that does not run is a failed run.")
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
                        help="DEPRECATED and ignored: a check that did not run "
                             "fails the run, always")
    parser.add_argument("--allow-missing-plugin", action="store_true",
                        help="DEPRECATED: a skip now fails the run too, so this "
                             "changes the label, never the outcome")
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
    if only and not any(row_selected(r["row"], only) for r in manifest["rows"]):
        print(f"[error] --rows {args.rows!r} selects no catalogue row at all. "
              f"Running nothing is not a pass.")
        return 2

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
            if borrowed_qgis(app):
                # Two reasons, both hard: the tier drives the shipped workers
                # SYNCHRONOUSLY (a live QGIS would freeze for the whole tier,
                # with per-row progress dialogs on top), and the console's
                # preloaded plugin wins every import, so the code under test
                # would be whatever was installed, not this repo.
                results.plan("-", "tierc.standalone")
                results.add("-", "tierc.standalone", "tier C runs standalone", FAIL,
                            "tier C cannot run inside a live QGIS: it drives the "
                            "GUI workers synchronously (this session would freeze "
                            "for the whole tier) and the console's loaded plugin "
                            "may not be this repo's. Run it standalone: "
                            "run_torture.bat --tier c   (at the repo root — it "
                            "finds python-qgis.bat itself; tiers a and b remain "
                            "console-runnable). To refresh the installed plugin "
                            "first: python deploy.py, then restart QGIS.")
            else:
                if app is None:
                    app = start_qgis()
                tier_pipeline(manifest, results, only, args.timeout,
                              args.keep_scratch)
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
    if incomplete:
        if args.allow_incomplete:
            print("[note] --allow-incomplete is deprecated and ignored.")
        print("[failed] checks were skipped or never ran. A check that does not "
              "run is a failed run — there is no flag that changes that.")
        return 1
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
    """Callable entry point, for when you want to re-run it from the console.

    Defaults to tiers a+b: tier C refuses to run inside a live QGIS (it
    drives the GUI workers synchronously and must test this repo's plugin,
    not the console's loaded copy) — run it standalone instead.
    """
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
