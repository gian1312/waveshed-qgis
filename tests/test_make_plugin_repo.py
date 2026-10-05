"""tools/make_plugin_repo.py — the custom QGIS repository listing + latest.json,
and package.py's trusted-manifest-key release guard."""

import hashlib
import json
import os
import sys
import xml.etree.ElementTree as ET
import zipfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "tools"))
sys.path.insert(0, _ROOT)

import make_plugin_repo as mpr  # noqa: E402
import package as pkg  # noqa: E402

META = """[general]
name=Waveshed
qgisMinimumVersion=3.28
supportsQt6=True
description=RF propagation & more <fast>
version={version}
author=Someone
email=a@b.c
about=About text with "quotes" & ampersand.
tracker=https://github.com/gian1312/waveshed-qgis/issues
repository=https://github.com/gian1312/waveshed-qgis
homepage=https://waveshed.io
tags=rf,gpu
changelog={version}
    - multi-line
      changelog with 100% literal percent
experimental=False
deprecated=False
"""


def _zip(tmp_path, version="1.2.3", name=None, meta=None):
    path = tmp_path / (name or f"waveshed.{version}.zip")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("waveshed/metadata.txt", meta if meta is not None else META.format(version=version))
        zf.writestr("waveshed/__init__.py", "")
    return str(path)


def test_both_listings_describe_the_zip(tmp_path):
    z = _zip(tmp_path)
    out = tmp_path / "out"
    assert mpr.main([z, "--out-dir", str(out), "--released-at", "2026-10-05"]) == 0

    latest = json.loads((out / "latest.json").read_text())
    with open(z, "rb") as fh:
        body = fh.read()
    assert latest == {
        "schema_version": 1, "version": "1.2.3", "released_at": "2026-10-05",
        "filename": "waveshed.1.2.3.zip",
        "url": "https://releases.waveshed.io/qgis/waveshed.1.2.3.zip",
        "size_bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(),
        "qgis_minimum_version": "3.28",
        "plugins_xml_url": "https://waveshed.io/qgis/plugins.xml",
    }

    root = ET.parse(out / "plugins.xml").getroot()
    assert root.tag == "plugins"
    (plugin,) = root.findall("pyqgis_plugin")
    assert plugin.get("name") == "Waveshed" and plugin.get("version") == "1.2.3"
    field = {child.tag: (child.text or "") for child in plugin}
    assert field["download_url"] == latest["url"]
    assert field["file_name"] == "waveshed.1.2.3.zip"
    assert field["description"] == "RF propagation & more <fast>"
    assert field["about"] == 'About text with "quotes" & ampersand.'
    assert field["qgis_minimum_version"] == "3.28"
    # Qt6-ready: QGIS 4 must see it (QGIS defaults a missing maximum to 3.99).
    assert field["qgis_maximum_version"] == "4.99"
    assert field["experimental"] == "False" and field["deprecated"] == "False"
    assert field["author_name"] == "Someone"
    for key in ("homepage", "tracker", "repository", "tags", "create_date", "update_date"):
        assert field[key], key


def test_experimental_flag_and_explicit_maximum_pass_through(tmp_path):
    meta = META.format(version="1.0.0").replace("experimental=False", "experimental=True") \
        + "qgisMaximumVersion=3.99\n"
    info = mpr.describe(_zip(tmp_path, "1.0.0", meta=meta), "2026-01-01")
    plugin = ET.fromstring(mpr.plugins_xml(info)).find("pyqgis_plugin")
    assert plugin.find("experimental").text == "True"
    assert plugin.find("qgis_maximum_version").text == "3.99"


def test_zip_name_must_match_its_own_metadata(tmp_path):
    z = _zip(tmp_path, "1.2.3", name="waveshed.1.2.4.zip")
    with pytest.raises(mpr.RepoError, match="must be 'waveshed.1.2.3.zip'"):
        mpr.describe(z, "2026-10-05")
    assert mpr.main([z, "--out-dir", str(tmp_path / "o")]) == 1
    assert not (tmp_path / "o").exists()


@pytest.mark.parametrize("bad", ["1.2", "v1.2.3", ""])
def test_version_must_be_semver(tmp_path, bad):
    meta = META.format(version=bad)
    with pytest.raises(mpr.RepoError):
        mpr.describe(_zip(tmp_path, name="waveshed.x.zip", meta=meta), "2026-10-05")


def test_zip_without_metadata_and_bad_date_are_errors(tmp_path):
    path = tmp_path / "waveshed.1.0.0.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("other/metadata.txt", "")
    with pytest.raises(mpr.RepoError, match="metadata.txt"):
        mpr.describe(str(path), "2026-10-05")
    with pytest.raises(mpr.RepoError, match="YYYY-MM-DD"):
        mpr.describe(_zip(tmp_path, "1.0.0"), "05.10.2026")


def test_real_plugin_metadata_is_describable(tmp_path):
    """The shipped metadata.txt must survive the generator (multi-line changelog etc.)."""
    meta = open(os.path.join(_ROOT, "waveshed", "metadata.txt"), encoding="utf-8").read()
    version = pkg.read_version(os.path.join(_ROOT, "waveshed", "metadata.txt"))
    info = mpr.describe(_zip(tmp_path, version, meta=meta), "2026-10-05")
    plugin = ET.fromstring(mpr.plugins_xml(info)).find("pyqgis_plugin")
    assert plugin.find("experimental").text == "False"
    assert plugin.find("repository").text == "https://github.com/gian1312/waveshed-qgis"


# --------------------------------------------------------------------------
# package.py: a public build must carry a trusted manifest key
# --------------------------------------------------------------------------

def _keys_file(root, body):
    path = root / "waveshed" / "core"
    path.mkdir(parents=True)
    (path / "release_keys.py").write_text(body, encoding="utf-8")
    return str(root)


def test_read_manifest_keys_forms(tmp_path):
    empty = _keys_file(tmp_path / "a", '"""doc"""\n\nMANIFEST_PUBLIC_KEYS: tuple[str, ...] = ()\n')
    assert pkg.read_manifest_keys(empty) == []
    one = _keys_file(tmp_path / "b",
                     'MANIFEST_PUBLIC_KEYS: tuple[str, ...] = (\n    "' + "ab" * 32 + '",\n)\n')
    assert pkg.read_manifest_keys(one) == ["ab" * 32]


@pytest.mark.parametrize("body", [
    'MANIFEST_PUBLIC_KEYS = ("ABAB",)\n',
    'MANIFEST_PUBLIC_KEYS = tuple(x)\n',
    'OTHER = ()\n',
])
def test_read_manifest_keys_rejects_malformed(tmp_path, body):
    with pytest.raises(pkg.BuildError):
        pkg.read_manifest_keys(_keys_file(tmp_path, body))


def test_release_needs_a_key_plain_build_only_warns(tmp_path, capsys):
    root = _keys_file(tmp_path, "MANIFEST_PUBLIC_KEYS: tuple[str, ...] = ()\n")
    with pytest.raises(pkg.BuildError, match="no trusted manifest-signing key"):
        pkg.check_manifest_keys(root, release=True)
    pkg.check_manifest_keys(root, release=False)
    assert "Do NOT publish" in capsys.readouterr().err


def test_shipped_release_keys_file_parses():
    keys = pkg.read_manifest_keys(_ROOT)
    assert isinstance(keys, list)
