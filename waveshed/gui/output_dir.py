"""Remembered "Output Dir" fields for the Site Analysis and P2P tabs.

The main dialog is deleted on close and rebuilt on every open, so a path held
only in the line edit is lost with the dialog: the field jumped back to the
temp default every time. Each tab now reads its folder from ``QgsSettings`` on
construction and writes it back whenever the user commits one (Browse,
finishing an edit, or starting a run with it).
"""

from __future__ import annotations

import os
import tempfile
from typing import Any

from qgis.core import QgsSettings

#: QgsSettings keys, one per tab (the two tabs keep separate folders).
SITE_OUTPUT_DIR_KEY = "waveshed/site_output_dir"
P2P_OUTPUT_DIR_KEY = "waveshed/p2p_output_dir"


def default_output_dir() -> str:
    """The fallback folder: ``aether_output`` in the system temp directory."""
    return os.path.join(tempfile.gettempdir(), "aether_output")


def load_output_dir(key: str) -> str:
    """The remembered folder for *key*, or the temp default when unset/empty."""
    value = str(QgsSettings().value(key, "") or "").strip()
    return value or default_output_dir()


def save_output_dir(key: str, path: str) -> None:
    """Remember *path* under *key*; a blank path is ignored (keeps the last one)."""
    path = (path or "").strip()
    if path:
        QgsSettings().setValue(key, path)


def bind_output_dir(line_edit: Any, key: str) -> None:
    """Fill *line_edit* from settings and save it whenever an edit is finished."""
    line_edit.setText(load_output_dir(key))
    line_edit.editingFinished.connect(
        lambda: save_output_dir(key, line_edit.text())
    )
