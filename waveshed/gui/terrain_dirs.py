"""Local terrain folders of the Site Analysis and P2P DEM pickers.

Both tabs' "DEM Layer:" combo lists, ahead of the project's raster layers,
the Settings → Local terrain dir (when set and existing), then the folders
picked with the "Folder…" button beside the combo. A folder entry is its path
string, shown as ``Local: <path>``: downstream every one is the same kind as
the Settings folder (a prepared ``.abt`` folder or a folder of raster files).

Picked folders are remembered in ONE list both tabs share
(:data:`RECENT_TERRAIN_DIRS_KEY`): most recent first, deduplicated by
normalised path, at most :data:`MAX_RECENT`; a folder that no longer exists is
dropped from it whenever a combo is filled. The main dialog is rebuilt on
every open, so this list is what brings a pick back (as ``output_dir`` does
for the output folders).
"""

from __future__ import annotations

import os
from typing import Any, Iterable, List, Optional

from qgis.core import QgsSettings
from qgis.PyQt.QtWidgets import QFileDialog

#: Settings → Local terrain dir (written by ``settings_dialog``).
SETTINGS_TERRAIN_DIR_KEY = "waveshed/terrain_dir"
#: The folders picked with "Folder…" in either tab, most recent first.
RECENT_TERRAIN_DIRS_KEY = "waveshed/terrain_dirs_recent"
#: How many picked folders are remembered.
MAX_RECENT = 8

#: The combo text of a folder entry is this prefix + the path.
LOCAL_DIR_PREFIX = "Local: "

#: The button beside each DEM combo, and its folder browser.
FOLDER_BUTTON_TEXT = "Folder…"
FOLDER_BUTTON_TOOLTIP = (
    "Choose a terrain folder in the file browser: DEM files (any GDAL "
    "elevation raster, subfolders included) or pre-built .abt tiles. It is "
    "selected at once and listed as \"Local: …\" in the DEM list of both "
    f"analysis tabs; the last {MAX_RECENT} folders are remembered."
)
DIALOG_TITLE = "Select Terrain Folder"


def _norm(path: str) -> str:
    """A folder's identity — the normalisation the folder index keys on
    (``terrain_index``), so ``C:/dem`` and ``c:\\dem\\`` are one folder on
    Windows."""
    return os.path.normcase(os.path.abspath(path))


def _stored() -> List[str]:
    """The recent list as stored. An empty list reads back as None (QGIS
    3.22, INI); a plain string counts as one folder (some PyQt builds hand a
    one-item list back that way)."""
    value = QgsSettings().value(RECENT_TERRAIN_DIRS_KEY, [])
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    try:
        paths = [str(v or "").strip() for v in value]
    except TypeError:  # anything else under the key: nothing remembered
        return []
    return [p for p in paths if p]


def _unique(paths: Iterable[str]) -> List[str]:
    """*paths* without a second entry for the same folder (first kept)."""
    seen = set()
    out = []
    for path in paths:
        key = _norm(path)
        if key not in seen:
            seen.add(key)
            out.append(path)
    return out


def recent_terrain_dirs() -> List[str]:
    """The remembered picks that still exist, most recent first; the stored
    list loses the folders that no longer exist."""
    stored = _stored()
    kept = [p for p in _unique(stored) if os.path.isdir(p)][:MAX_RECENT]
    if kept != stored:
        QgsSettings().setValue(RECENT_TERRAIN_DIRS_KEY, kept)
    return kept


def remember_terrain_dir(path: str) -> None:
    """Put *path* first in the recent list: an older entry for the same
    folder goes, folders that no longer exist go, at most :data:`MAX_RECENT`
    stay. A blank path changes nothing."""
    path = (path or "").strip()
    if not path:
        return
    kept = [p for p in _unique([path] + _stored()) if os.path.isdir(p)]
    QgsSettings().setValue(RECENT_TERRAIN_DIRS_KEY, kept[:MAX_RECENT])


def settings_terrain_dir() -> str:
    """Settings → Local terrain dir, "" when unset."""
    return str(QgsSettings().value(SETTINGS_TERRAIN_DIR_KEY, "") or "").strip()


def combo_folders() -> List[str]:
    """The folder entries of a DEM combo, in order: the Settings folder (when
    set and existing), then the recent picks without the Settings folder."""
    folders = []
    configured = settings_terrain_dir()
    if configured and os.path.isdir(configured):
        folders.append(configured)
    taken = {_norm(p) for p in folders}
    folders.extend(p for p in recent_terrain_dirs() if _norm(p) not in taken)
    return folders


def entry_label(path: str) -> str:
    """The combo text of the folder entry *path*."""
    return f"{LOCAL_DIR_PREFIX}{path}"


def add_folder_entries(combo: Any, entries: list) -> None:
    """Append :func:`combo_folders` to a DEM *combo* and to its parallel list
    of *entries* (a folder entry is its path string)."""
    for folder in combo_folders():
        combo.addItem(entry_label(folder))
        entries.append(folder)


def folder_index(entries: list, path: Optional[str]) -> int:
    """Index of the folder entry for the same folder as *path*, else -1."""
    if not path:
        return -1
    key = _norm(path)
    for i, entry in enumerate(entries):
        if isinstance(entry, str) and _norm(entry) == key:
            return i
    return -1


def dialog_start_dir(selected: Optional[str] = None) -> str:
    """Where the folder browser opens: the *selected* folder, else the last
    folder picked, else the home folder."""
    if selected and os.path.isdir(selected):
        return selected
    recent = recent_terrain_dirs()
    return recent[0] if recent else os.path.expanduser("~")


def ask_terrain_dir(parent: Any, selected: Optional[str] = None) -> str:
    """The "Folder…" button: ask for a terrain folder.

    Returns the folder chosen, already remembered first in the recent list,
    or "" when the browser was cancelled (then nothing is stored)."""
    path = QFileDialog.getExistingDirectory(
        parent, DIALOG_TITLE, dialog_start_dir(selected))
    path = str(path or "").strip()
    if path:
        remember_terrain_dir(path)
    return path


def announce_pick(dialog: Any, tab: Any) -> None:
    """Tell the main *dialog* that *tab* picked a folder, so the other
    analysis tab lists it at once (``terrain_dir_picked_in``)."""
    notify = getattr(dialog, "terrain_dir_picked_in", None)
    if callable(notify):
        notify(tab)
