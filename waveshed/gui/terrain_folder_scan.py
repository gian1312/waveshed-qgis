"""GUI side of the terrain-folder index: scan off the GUI thread, then ask.

Shared by Site Analysis (Run) and the Map Converter (Add Folder / Convert):

* :func:`scan_folder` refreshes ``core/terrain_index`` for a folder in a
  :class:`FolderScanThread` while a progress dialog keeps the GUI live, and
  can run more work on the index in that same thread (the coverage check) —
  nothing that opens files ever runs on the GUI thread (2,500 files took
  84 s of frozen UI through the old BuildVRT check).
* :func:`confirm_problem_files` is the ONE pre-flight prompt: every file
  that cannot be used, with its reason, "Skip N unusable files and
  continue?", default Cancel. Skipped files are logged.
* :func:`ask_crs_for_files` (Map Converter) — CRS-less files never inherit
  another file's CRS silently; the user assigns one (the folder's detected
  CRS offered as the suggestion), per file or for all, or skips them.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Callable, Dict, List, Optional, Tuple

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


_PHASES = {"listing": "Listing files", "checking": "Checking for changes",
           "reading": "Reading new or changed files", "done": "Done"}


def scan_folder(parent: Any, folder: str,
                after: Optional[Callable[[Any], Any]] = None,
                title: str = "Reading terrain folder",
                refresh: bool = True) -> Optional[Dict[str, Any]]:
    """Scan *folder* in a worker thread behind a cancellable progress dialog.

    Returns ``{"index", "stats", "extra"}`` (``extra`` = *after(index)*,
    computed in the worker thread too), or None when the user cancelled.
    A failure is raised as RuntimeError. *refresh* False runs only *after*
    (e.g. the coverage check right after a scan).
    """
    from qgis.PyQt.QtCore import QEventLoop

    thread = FolderScanThread(folder, after, parent, refresh=refresh)
    dlg = QProgressDialog(f"{title}…", "Cancel", 0, 0, parent)
    dlg.setWindowTitle("Waveshed")
    dlg.setWindowModality(Qt.WindowModality.WindowModal)
    dlg.setMinimumDuration(500)
    dlg.setAutoClose(False)
    dlg.setAutoReset(False)

    def on_progress(done: int, total: int, phase: str) -> None:
        dlg.setLabelText(f"{title}…\n{_PHASES.get(phase, phase)}"
                         + (f": {done:,} / {total:,}" if total else ""))
        dlg.setMaximum(max(total, 0))
        dlg.setValue(min(done, total) if total else 0)

    loop = QEventLoop()
    thread.progress.connect(on_progress)
    thread.finished.connect(loop.quit)
    dlg.canceled.connect(thread.cancel)
    thread.start()
    if thread.isRunning():
        loop.exec()
    thread.wait()
    dlg.close()
    if thread.error is not None:
        raise RuntimeError(thread.error)
    result = thread.result
    if result is not None and result["stats"] is not None:
        st = result["stats"]
        _log(f"terrain folder index {folder}: {st.files} file(s) — {st.new} "
             f"new, {st.changed} changed, {st.removed} removed, "
             f"{st.unchanged} unchanged; {st.total_s:.1f}s")
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
