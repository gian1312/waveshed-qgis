#!/usr/bin/env python3
"""Write the Waveshed custom QGIS plugin repository files for one plugin ZIP.

Given ``dist/waveshed.<version>.zip`` (built by ``package.py``) this writes,
into ``--out-dir``:

``plugins.xml``
    A QGIS plugin repository listing with one ``<pyqgis_plugin>`` — the
    release in the ZIP. Users add ``https://waveshed.io/qgis/plugins.xml``
    once under Plugins -> Manage and Install Plugins -> Settings -> Add, and
    QGIS then offers installs and updates from it.
``latest.json``
    The same release for the waveshed.io downloads page::

        {"schema_version": 1, "version", "released_at": "YYYY-MM-DD",
         "filename", "url", "size_bytes", "sha256", "qgis_minimum_version",
         "plugins_xml_url"}

Every value is read from ``waveshed/metadata.txt`` INSIDE the ZIP (not from
the working tree), so the listing can never describe a different build than
the one uploaded; the ZIP's file name must match that version.

Hosting (the release workflow uploads them): the ZIP to
``https://releases.waveshed.io/qgis/<filename>`` (immutable), both listings to
``qgis/plugins.xml`` / ``qgis/latest.json`` in the same R2 bucket, relayed by
waveshed.io at ``/qgis/plugins.xml`` and ``/qgis/latest.json``.

Usage: ``python tools/make_plugin_repo.py ZIP --out-dir DIR [--released-at YYYY-MM-DD]``

Standard library only.
"""

from __future__ import annotations

import argparse
import configparser
import datetime
import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from typing import Dict, List, Optional

PACKAGE_NAME = "waveshed"
DOWNLOAD_BASE_URL = "https://releases.waveshed.io/qgis/"
PLUGINS_XML_URL = "https://waveshed.io/qgis/plugins.xml"
DEFAULT_UPLOADED_BY = "Waveshed release workflow"


class RepoError(Exception):
    """The ZIP cannot be described (wrong name, missing metadata, bad version)."""


def read_zip_metadata(zip_path: str) -> Dict[str, str]:
    """``[general]`` of ``waveshed/metadata.txt`` inside *zip_path* (keys lower-cased)."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            raw = zf.read(f"{PACKAGE_NAME}/metadata.txt").decode("utf-8")
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise RepoError(f"{zip_path}: no readable {PACKAGE_NAME}/metadata.txt ({exc})") from exc
    parser = configparser.RawConfigParser()
    parser.read_string(raw)
    if not parser.has_section("general"):
        raise RepoError(f"{zip_path}: metadata.txt has no [general] section")
    return {k: v.strip() for k, v in parser.items("general")}


def _bool_text(value: Optional[str]) -> str:
    return "True" if str(value or "").strip().lower() in ("true", "1", "yes") else "False"


def qgis_maximum_version(meta: Dict[str, str]) -> str:
    """``qgisMaximumVersion`` if set; else 4.99 for a Qt6-ready plugin.

    QGIS fills a missing maximum with ``<minimum major>.99``, which would hide
    a ``supportsQt6=True`` plugin from QGIS 4 users of this repository.
    """
    explicit = meta.get("qgismaximumversion")
    if explicit:
        return explicit
    if _bool_text(meta.get("supportsqt6")) == "True":
        return "4.99"
    major = (meta.get("qgisminimumversion") or "3").split(".")[0]
    return f"{major}.99"


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def describe(zip_path: str, released_at: str) -> Dict[str, object]:
    """Everything both listings need, validated."""
    meta = read_zip_metadata(zip_path)
    version = meta.get("version", "")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise RepoError(f"metadata version {version!r} is not X.Y.Z")
    filename = os.path.basename(zip_path)
    expected = f"{PACKAGE_NAME}.{version}.zip"
    if filename != expected:
        raise RepoError(f"ZIP is named {filename!r}; its metadata says it must be {expected!r}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", released_at):
        raise RepoError(f"--released-at {released_at!r} is not YYYY-MM-DD")
    for key in ("name", "description", "about", "author", "qgisminimumversion"):
        if not meta.get(key):
            raise RepoError(f"metadata.txt has no {key}=")
    return {
        "meta": meta,
        "version": version,
        "filename": filename,
        "url": DOWNLOAD_BASE_URL + filename,
        "size_bytes": os.path.getsize(zip_path),
        "sha256": _sha256(zip_path),
        "released_at": released_at,
    }


def plugins_xml(info: Dict[str, object], uploaded_by: str = DEFAULT_UPLOADED_BY) -> bytes:
    """The QGIS repository XML for *info* (from :func:`describe`)."""
    meta = info["meta"]
    root = ET.Element("plugins")
    plugin = ET.SubElement(root, "pyqgis_plugin", {
        "name": meta["name"],
        "version": info["version"],
        "plugin_id": PACKAGE_NAME,
    })

    def add(tag: str, text: Optional[str]) -> None:
        ET.SubElement(plugin, tag).text = text or ""

    add("description", meta.get("description"))
    add("about", meta.get("about"))
    add("version", info["version"])
    add("qgis_minimum_version", meta.get("qgisminimumversion"))
    add("qgis_maximum_version", qgis_maximum_version(meta))
    add("homepage", meta.get("homepage"))
    add("file_name", info["filename"])
    add("author_name", meta.get("author"))
    add("download_url", info["url"])
    add("uploaded_by", uploaded_by)
    add("create_date", info["released_at"])
    add("update_date", info["released_at"])
    add("experimental", _bool_text(meta.get("experimental")))
    add("deprecated", _bool_text(meta.get("deprecated")))
    add("tracker", meta.get("tracker"))
    add("repository", meta.get("repository"))
    add("tags", meta.get("tags"))
    add("server", "False")
    if hasattr(ET, "indent"):  # Python 3.9+
        ET.indent(root)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8") + b"\n"


def latest_json(info: Dict[str, object]) -> bytes:
    """The downloads-page record for *info* (from :func:`describe`)."""
    doc = {
        "schema_version": 1,
        "version": info["version"],
        "released_at": info["released_at"],
        "filename": info["filename"],
        "url": info["url"],
        "size_bytes": info["size_bytes"],
        "sha256": info["sha256"],
        "qgis_minimum_version": info["meta"].get("qgisminimumversion"),
        "plugins_xml_url": PLUGINS_XML_URL,
    }
    return (json.dumps(doc, indent=2) + "\n").encode("utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("zip", help="dist/waveshed.<version>.zip")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--released-at",
                        default=datetime.datetime.now(datetime.timezone.utc).date().isoformat())
    parser.add_argument("--uploaded-by", default=DEFAULT_UPLOADED_BY)
    args = parser.parse_args(argv)
    try:
        info = describe(args.zip, args.released_at)
    except RepoError as exc:
        print(f"make_plugin_repo: {exc}", file=sys.stderr)
        return 1
    os.makedirs(args.out_dir, exist_ok=True)
    for name, body in (("plugins.xml", plugins_xml(info, args.uploaded_by)),
                       ("latest.json", latest_json(info))):
        path = os.path.join(args.out_dir, name)
        with open(path, "wb") as fh:
            fh.write(body)
        print(f"wrote {path}")
    print(f"version={info['version']} sha256={info['sha256']} size={info['size_bytes']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
