"""Pins the waveshed.io page links and their private-preview flag.

``/downloads`` and ``/get-key`` are a private preview on waveshed.io: without
``?prv`` they show a placeholder / 404. Every plugin link to them is built by
``core/site_links.py`` so the flag can be dropped in one place at release
(TODO(release): remove ?prv before publication).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from waveshed.core import api_key, site_links

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PKG = PLUGIN_ROOT / "waveshed"


def test_helper_appends_preview_flag():
    assert site_links.SITE_URL == "https://waveshed.io"
    assert site_links.PREVIEW_QUERY == "?prv"  # TODO(release): expect ""
    assert site_links.site_page_url("/downloads") == "https://waveshed.io/downloads?prv"


def test_helper_rejects_relative_paths():
    with pytest.raises(ValueError):
        site_links.site_page_url("get-key")


def test_get_api_key_button_target():
    # The "Get API Key" button opens api_key.GET_API_KEY_URL: the key generator.
    assert api_key.GET_API_KEY_URL == "https://waveshed.io/get-key?prv"
    assert api_key.GET_API_KEY_URL == site_links.GET_KEY_URL
    src = (PKG / "gui" / "settings_dialog.py").read_text(encoding="utf-8")
    assert 'QPushButton("Get API Key")' in src
    assert "QUrl(api_key.GET_API_KEY_URL)" in src


def test_missing_key_message_points_at_key_page():
    ok, msg, _ = api_key.validate_api_key("")
    assert not ok
    assert site_links.GET_KEY_URL in msg


def test_help_links_key_page_through_helper():
    from waveshed.help.content import TOPICS

    install = next(t for t in TOPICS if t.anchor == "install")
    assert f'href="{site_links.GET_KEY_URL}"' in install.body
    assert "{GET_KEY_URL}" not in install.body


# Gated pages must never be hardcoded outside site_links.py, or the release
# switch would miss them.
_GATED = re.compile(r"waveshed\.io/(?:downloads|get-key)\b")


def test_no_hardcoded_gated_links():
    offenders = []
    for path in PKG.rglob("*"):
        if path.suffix not in {".py", ".txt", ".html", ".md", ".json"}:
            continue
        if path.name == "site_links.py" or "__pycache__" in path.parts:
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # Display text next to a helper-built href is fine; a URL is not.
            if _GATED.search(line) and ("https://" in line or "http://" in line):
                offenders.append(f"{path.relative_to(PLUGIN_ROOT)}:{i}")
    assert not offenders, offenders


def test_package_warns_about_preview_links(tmp_path, capsys):
    import package as pkg

    clean = tmp_path / "clean.py"
    clean.write_text('URL = "https://waveshed.io/downloads"\n', encoding="utf-8")
    flagged = tmp_path / "links.py"
    flagged.write_text('Q = "?prv"\n', encoding="utf-8")
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"?prv")  # binary assets are not scanned

    entries = [
        (str(clean), "waveshed/clean.py"),
        (str(flagged), "waveshed/core/links.py"),
        (str(icon), "waveshed/resources/icon.png"),
    ]
    hits = pkg.find_preview_links(entries)
    assert hits == ["waveshed/core/links.py"]

    pkg._warn_preview_links(hits)
    err = capsys.readouterr().err
    assert "WARNING" in err and "waveshed/core/links.py" in err


def test_current_plugin_is_flagged_by_package():
    # While the preview flag is set, the real package must trip the warning.
    import package as pkg

    entries = list(pkg.iter_plugin_files(str(PKG), str(PLUGIN_ROOT)))
    hits = [h.replace("\\", "/") for h in pkg.find_preview_links(entries)]
    if site_links.PREVIEW_QUERY:
        assert "waveshed/core/site_links.py" in hits
    else:
        assert hits == []
