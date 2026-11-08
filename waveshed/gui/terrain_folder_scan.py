"""GUI side of the terrain-folder index: scan off the GUI thread, then ask.

Shared by Site Analysis (Run) and the Map Converter (Add Folder / Convert):

* :func:`scan_folder` refreshes ``core/terrain_index`` for a folder in a
  :class:`FolderScanThread` while a progress dialog keeps the GUI live, and
  can run more work on the index in that same thread (the coverage check) —
  nothing that opens files ever runs on the GUI thread (2,500 files took
  84 s of frozen UI through the old BuildVRT check). On Windows, when the
  bulk read of a cold scan starts (:data:`COLD_SCAN_NOTE_MIN_FILES` or more
  files), the dialog adds ONE note — :data:`COLD_SCAN_NOTE`, Defender and
  the exclusions the user may set — logged once per folder. Nothing here
  queries or changes Windows Defender; off Windows there is no note at all.
* :func:`confirm_problem_files` is the ONE pre-flight prompt: every file
  that cannot be used, with its reason, "Skip N unusable files and
  continue?", default Cancel. Skipped files are logged.
* :func:`ask_crs_for_files` (Map Converter) — CRS-less files never inherit
  another file's CRS silently; the user assigns one (the folder's detected
  CRS offered as the suggestion), per file or for all, or skips them.
* :func:`run_task` runs other folder work the same way — its own
  :class:`TaskThread` behind the same cancellable progress dialog (the Map
  Converter's Inspect .abt: the header index, then the mosaic).
"""

from __future__ import annotations

import sys
import textwrap
from collections import Counter
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from qgis.PyQt.QtCore import QThread, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import QInputDialog, QMessageBox, QProgressDialog

from ..core import terrain_index


def _log(msg: str) -> None:
    try:
        from qgis.core import Qgis, QgsMessageLog
        QgsMessageLog.logMessage(msg, "Waveshed-Terrain", Qgis.MessageLevel.Info)
    except Exception:  # noqa: BLE001
        pass


class FolderScanThread(QThread):
    """Refresh one folder index (and optionally run *after(index)*) off the
    GUI thread."""

    progress = pyqtSignal(int, int, str)
    scan_ok = pyqtSignal(object)
    scan_err = pyqtSignal(str)

    def __init__(self, folder: str, after: Optional[Callable[[Any], Any]] = None,
                 parent: Any = None, refresh: bool = True) -> None:
        super().__init__(parent)
        self._folder = folder
        self._refresh = refresh
        self._after = after
        self._cancel = False
        self.result: Optional[Dict[str, Any]] = None
        self.error: Optional[str] = None

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:  # noqa: D401 — Qt override
        try:
            index = terrain_index.get_index(self._folder)
            stats = (index.refresh(progress=self.progress.emit,
                                   should_cancel=lambda: self._cancel)
                     if self._refresh else None)
            extra = self._after(index) if self._after is not None else None
            self.result = {"index": index, "stats": stats, "extra": extra}
            self.scan_ok.emit(self.result)
        except terrain_index.ScanCancelled:
            self.result = None
            self.scan_ok.emit(None)
        except Exception as exc:  # noqa: BLE001 — reported to the caller
            self.error = str(exc)
            self.scan_err.emit(self.error)


class TaskThread(QThread):
    """Run *work(progress, should_cancel)* off the GUI thread — the scan's
    machinery for other folder work. *progress(done, total, text)* drives
    the dialog; *should_cancel()* is True once Cancel was pressed. The
    result is :attr:`result`; a failure's text :attr:`error`."""

    progress = pyqtSignal(int, int, str)

    def __init__(self, work: Callable[[Callable[[int, int, str], Any],
                                       Callable[[], bool]], Any],
                 parent: Any = None) -> None:
        super().__init__(parent)
        self._work = work
        self._cancel = False
        self.result: Any = None
        self.error: Optional[str] = None

    def cancel(self) -> None:
        self._cancel = True

    def cancelled(self) -> bool:
        return self._cancel

    def run(self) -> None:  # noqa: D401 — Qt override
        try:
            self.result = self._work(self.progress.emit, self.cancelled)
        except Exception as exc:  # noqa: BLE001 — reported to the caller
            self.error = str(exc) or type(exc).__name__


def _progress_dialog(parent: Any, title: str) -> Any:
    """The cancellable, window-modal progress dialog of a folder task
    (shown after 0.5 s; closed by the caller)."""
    dlg = QProgressDialog(f"{title}…", "Cancel", 0, 0, parent)
    dlg.setWindowTitle("Waveshed")
    dlg.setWindowModality(Qt.WindowModality.WindowModal)
    dlg.setMinimumDuration(500)
    dlg.setAutoClose(False)
    dlg.setAutoReset(False)
    return dlg


def _run_behind(thread: Any, dlg: Any) -> None:
    """Start *thread* and keep the GUI live (a local event loop) until it
    has finished; Cancel on *dlg* asks the thread to stop."""
    from qgis.PyQt.QtCore import QEventLoop

    loop = QEventLoop()
    thread.finished.connect(loop.quit)
    dlg.canceled.connect(thread.cancel)
    thread.start()
    if thread.isRunning():
        loop.exec()
    thread.wait()
    dlg.close()


def run_task(parent: Any, title: str,
             work: Callable[[Callable[[int, int, str], Any],
                             Callable[[], bool]], Any]) -> Any:
    """Run *work(progress, should_cancel)* in a :class:`TaskThread` behind
    the cancellable progress dialog (*title*; ``progress(done, total,
    text)`` sets ``text: done / total`` under it, a busy bar while *total*
    is 0). Returns what *work* returned — None when it stopped for Cancel —
    and raises a failure as RuntimeError, like :func:`scan_folder`."""
    thread = TaskThread(work, parent)
    dlg = _progress_dialog(parent, title)

    def on_progress(done: int, total: int, text: str) -> None:
        dlg.setLabelText(f"{title}…\n{text}"
                         + (f": {done:,} / {total:,}" if total else ""))
        dlg.setMaximum(max(total, 0))
        dlg.setValue(min(done, total) if total else 0)

    thread.progress.connect(on_progress)
    _run_behind(thread, dlg)
    if thread.error is not None:
        raise RuntimeError(thread.error)
    return thread.result


_PHASES = {"listing": "Listing files", "checking": "Checking for changes",
           "reading": "Reading new or changed files",
           "indexing": "Building the index", "done": "Done"}

# ---------------------------------------------------------------------------
# The cold-scan note (Windows only). Real-time protection scans every file
# the first time it is opened: 2-5 ms per file on a typical laptop, ~86 ms
# measured on the owner's machine (58 s of a 60 s cold scan of 43,650
# files). The plugin only SAYS so, in the progress dialog of the bulk read —
# it never queries or changes Defender (owner decision 2026-10-23: the
# exclusion offer, its status queries and its administrator prompt are gone).
# ---------------------------------------------------------------------------

#: A bulk read ("reading" phase of a refresh: the converter probe's list or
#: the in-process probes) of at least this many files gets the note. At the
#: typical 2-5 ms Defender adds per first open that is 2-5 s of the scan
#: (~86 s on the owner's machine) — below it the dialog is gone before the
#: note could be read.
COLD_SCAN_NOTE_MIN_FILES = 1000

#: The note: one line of the dialog (wrapped there) and of the log.
COLD_SCAN_NOTE = (
    "Windows Defender scans every file on first open. For large folders, "
    "exclude this folder and the Waveshed terrain cache from real-time "
    "scanning (Windows Security → Virus & threat protection → "
    "Exclusions).")

#: Width the note is wrapped to in the progress dialog (one unwrapped line
#: would stretch it across the screen).
_NOTE_WIDTH = 72

#: ``terrain_index.folder_key`` of every folder whose note was logged in
#: this session (logged once per folder).
_NOTE_LOGGED: Set[str] = set()


def _is_windows() -> bool:
    """The note's one gate: ``sys.platform`` (as
    ``terrain_adapter._is_windows``)."""
    return sys.platform == "win32"


def cold_scan_note(total: int, phase: str) -> str:
    """:data:`COLD_SCAN_NOTE` for a progress tick that starts (or continues)
    the bulk read — *phase* "reading" — of *total* >=
    :data:`COLD_SCAN_NOTE_MIN_FILES` files on Windows; "" otherwise (always
    "" off Windows)."""
    if (not _is_windows() or phase != "reading"
            or total < COLD_SCAN_NOTE_MIN_FILES):
        return ""
    return COLD_SCAN_NOTE


def _log_cold_scan_note(folder: str) -> None:
    """The note in the log, once per folder per session (Info)."""
    key = terrain_index.folder_key(folder)
    if key in _NOTE_LOGGED:
        return
    _NOTE_LOGGED.add(key)
    try:
        from ..core.terrain_adapter import get_cache_dir
        cache = f"; terrain cache: {get_cache_dir()}"
    except Exception:  # noqa: BLE001 — the note goes out without the path
        cache = ""
    _log(f"{COLD_SCAN_NOTE} (folder: {folder}{cache})")


def scan_folder(parent: Any, folder: str,
                after: Optional[Callable[[Any], Any]] = None,
                title: str = "Reading terrain folder",
                refresh: bool = True) -> Optional[Dict[str, Any]]:
    """Scan *folder* in a worker thread behind a cancellable progress dialog.

    Returns ``{"index", "stats", "extra"}`` (``extra`` = *after(index)*,
    computed in the worker thread too), or None when the user cancelled.
    A failure is raised as RuntimeError. *refresh* False runs only *after*
    (e.g. the coverage check right after a scan). On Windows the dialog
    carries :data:`COLD_SCAN_NOTE` from the first tick of a bulk read of
    :data:`COLD_SCAN_NOTE_MIN_FILES` or more files to the end of the scan
    (:func:`cold_scan_note`).
    """
    thread = FolderScanThread(folder, after, parent, refresh=refresh)
    dlg = _progress_dialog(parent, title)
    note = ""

    def on_progress(done: int, total: int, phase: str) -> None:
        nonlocal note
        if not note:
            note = cold_scan_note(total, phase)
            if note:
                _log_cold_scan_note(folder)
        dlg.setLabelText(f"{title}…\n{_PHASES.get(phase, phase)}"
                         + (f": {done:,} / {total:,}" if total else "")
                         + (f"\n\n{textwrap.fill(note, _NOTE_WIDTH)}"
                            if note else ""))
        dlg.setMaximum(max(total, 0))
        dlg.setValue(min(done, total) if total else 0)

    thread.progress.connect(on_progress)
    _run_behind(thread, dlg)
    if thread.error is not None:
        raise RuntimeError(thread.error)
    result = thread.result
    # The finished thread lives on under its Qt parent: it must not keep
    # what *after* returned (Site Analysis: the run's TerrainPrep, ~27 MB
    # for 43,650 files) beyond this call.
    thread.result = None
    thread._after = None
    if result is not None and result["stats"] is not None:
        st = result["stats"]
        _log(f"terrain folder index {folder}: {st.files} file(s) — {st.new} "
             f"new, {st.changed} changed, {st.removed} removed, "
             f"{st.unchanged} unchanged; {st.total_s:.1f}s"
             + (" (headers read by aether_converter probe)"
                if getattr(st, "probe_engine", "") == "converter" else ""))
    return result


def confirm_problem_files(parent: Any, folder: str,
                          problems: List[Tuple[str, str, str]],
                          ) -> Optional[Dict[str, str]]:
    """The pre-flight prompt for files that cannot be used as they are.

    Returns ``{path: "skip" | "use"}`` (empty when there is nothing to ask),
    or None when the user cancelled (the default button). 8-bit single-band
    files can be kept as elevation with a second button; every other
    problem can only be skipped.
    """
    if not problems:
        return {}
    suspects = [p for p in problems if p[1] == terrain_index.KIND_SUSPECT]
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("Unusable terrain files")
    box.setText(f"Skip {len(problems)} unusable file(s) and continue?")
    box.setInformativeText(
        f"In {folder}:\n\n"
        + terrain_index.format_problems(problems, folder, limit=10)
        + "\n\nSkipped files are left out of this run (and logged); "
          "their ground is covered by the other files or built as 0 m.")
    box.setDetailedText("\n".join(
        f"{terrain_index.display_path(p, folder)} — {reason}"
        for p, _k, reason in problems))
    btn_skip = box.addButton(f"Skip {len(problems)} file(s)",
                             QMessageBox.ButtonRole.AcceptRole)
    btn_use = None
    if suspects:
        btn_use = box.addButton(
            f"Use the {len(suspects)} 8-bit file(s) as elevation",
            QMessageBox.ButtonRole.ActionRole)
    btn_cancel = box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(btn_cancel)
    box.setEscapeButton(btn_cancel)
    box.exec()
    clicked = box.clickedButton()
    if clicked is btn_skip:
        decisions = {p: terrain_index.DECISION_SKIP for p, _k, _r in problems}
    elif btn_use is not None and clicked is btn_use:
        decisions = {p: (terrain_index.DECISION_USE
                         if k == terrain_index.KIND_SUSPECT
                         else terrain_index.DECISION_SKIP)
                     for p, k, _r in problems}
    else:
        return None
    for p, _k, reason in problems:
        if decisions[p] == terrain_index.DECISION_SKIP:
            _log(f"  skipping {p} — {reason}")
        else:
            _log(f"  using {p} as elevation although it is 8-bit")
    return decisions


def suggested_crs(index: Any) -> Optional[str]:
    """The folder's dominant EPSG authid (offered for CRS-less files)."""
    counts = Counter(e.get("crs_authid") for e in index.entries.values()
                     if e.get("readable") and e.get("crs_authid"))
    return counts.most_common(1)[0][0] if counts else None


_COMMON_CRS = ["EPSG:4326", "EPSG:2056", "EPSG:21781", "EPSG:25832",
               "EPSG:25833", "EPSG:32632", "EPSG:32633", "EPSG:3857"]


def _ask_one(parent: Any, label: str, suggestion: Optional[str]) -> Optional[str]:
    items = ([suggestion] if suggestion else []) + [
        c for c in _COMMON_CRS if c != suggestion]
    while True:
        text, ok = QInputDialog.getItem(parent, "Assign a CRS", label, items,
                                        0, True)
        if not ok:
            return None
        text = (text or "").strip()
        if text and terrain_index.crs_is_usable(text):
            return text
        QMessageBox.warning(parent, "Invalid CRS",
                            f"'{text}' is not a coordinate system GDAL "
                            "knows. Enter e.g. EPSG:2056.")


def ask_crs_for_files(parent: Any, folder: str, paths: List[str],
                      suggestion: Optional[str]) -> Optional[Dict[str, str]]:
    """Ask which CRS the CRS-less *paths* are in.

    Returns ``{path: crs}`` for the files the user georeferenced (files left
    out are skipped by the caller), or None on Cancel (the default).
    """
    if not paths:
        return {}
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle("Files without a coordinate system")
    box.setText(f"{len(paths)} file(s) in this folder have no coordinate "
                "system (CRS).")
    shown = "\n".join(f"• {terrain_index.display_path(p, folder)}"
                      for p in paths[:10])
    if len(paths) > 10:
        shown += f"\n• … and {len(paths) - 10} more"
    box.setInformativeText(
        f"{shown}\n\nThey are never given another file's CRS automatically "
        "— a wrong guess places terrain kilometres off. "
        + (f"The other files in the folder are in {suggestion}."
           if suggestion else ""))
    btn_all = None
    if suggestion:
        btn_all = box.addButton(f"Use {suggestion} for all",
                                QMessageBox.ButtonRole.AcceptRole)
    btn_each = box.addButton("Choose per file…", QMessageBox.ButtonRole.ActionRole)
    btn_skip = box.addButton("Skip them", QMessageBox.ButtonRole.RejectRole)
    btn_cancel = box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(btn_cancel)
    box.setEscapeButton(btn_cancel)
    box.exec()
    clicked = box.clickedButton()
    if btn_all is not None and clicked is btn_all:
        out = {p: suggestion for p in paths}
    elif clicked is btn_each:
        out = {}
        for p in paths:
            crs = _ask_one(parent, f"CRS of {terrain_index.display_path(p, folder)}"
                                   "\n(Cancel = skip this file):", suggestion)
            if crs:
                out[p] = crs
    elif clicked is btn_skip:
        out = {}
    else:
        return None
    for p in paths:
        _log(f"  {p}: " + (f"CRS {out[p]} assigned by the user" if p in out
                           else "no CRS — skipped"))
    return out


def resolve_folder_problems(parent: Any, folder: str, index: Any,
                            crs_overrides: Optional[Dict[str, str]] = None,
                            decisions: Optional[Dict[str, str]] = None,
                            ask_crs: bool = False,
                            ) -> Optional[Tuple[Dict[str, str], Dict[str, str]]]:
    """Ask about every problem file not answered yet.

    Returns ``(crs_overrides, decisions)`` (merged with the given ones) or
    None when the user cancelled. With *ask_crs* (Map Converter), CRS-less
    files get the CRS prompt first; otherwise (Site Analysis) they are
    listed in the skip prompt like any other unusable file.
    """
    crs_overrides = dict(crs_overrides or {})
    decisions = dict(decisions or {})
    open_ = terrain_index.unresolved_problems(index, decisions, crs_overrides)
    if ask_crs:
        nocrs = [p for p, k, _r in open_ if k == terrain_index.KIND_NOCRS]
        if nocrs:
            got = ask_crs_for_files(parent, folder, nocrs, suggested_crs(index))
            if got is None:
                return None
            # A CRS the file's coordinates cannot be in (LV95 metres read as
            # degrees) is refused here, not discovered as misplaced terrain.
            misfit = [p for p, crs in got.items()
                      if terrain_index.with_crs(index.entries[p], crs) is None]
            if misfit:
                QMessageBox.warning(
                    parent, "CRS does not fit",
                    f"The assigned CRS does not fit {len(misfit)} file(s) — "
                    "their coordinates lie outside it — so they are skipped:"
                    "\n\n" + "\n".join(
                        f"• {terrain_index.display_path(p, folder)} "
                        f"({got[p]})" for p in misfit[:10]))
                for p in misfit:
                    _log(f"  {p}: CRS {got.pop(p)} does not fit — skipped")
            crs_overrides.update(got)
            for p in nocrs:
                if p not in got:
                    decisions[p] = terrain_index.DECISION_SKIP
            open_ = terrain_index.unresolved_problems(index, decisions,
                                                      crs_overrides)
    answer = confirm_problem_files(parent, folder, open_)
    if answer is None:
        return None
    decisions.update(answer)
    return crs_overrides, decisions
