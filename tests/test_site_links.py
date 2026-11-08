"""Pins the waveshed.io page links and the preview gate they live behind.

``/downloads`` and ``/get-key`` are gated on waveshed.io until the public
launch: without ``?prv`` they show a placeholder / 404. Every plugin link to
them is built by ``core/site_links.py``: static text carries ``?prv`` (harmless
once the gate is gone), and what a button opens — ``site_links.page_url`` —
probes the site once per session and drops the flag as soon as the pages are
public. The plugin therefore works on both sides of the launch without a new
release.
"""

from __future__ import annotations

import io
import re
import urllib.error
from pathlib import Path

import pytest

from waveshed.core import api_key, site_links

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PKG = PLUGIN_ROOT / "waveshed"


@pytest.fixture(autouse=True)
def _fresh_gate():
    site_links.reset_gate()
    yield
    site_links.reset_gate()


def test_helper_appends_preview_flag():
    assert site_links.SITE_URL == "https://waveshed.io"
    assert site_links.PREVIEW_QUERY == "?prv"
    assert site_links.site_page_url("/downloads") == "https://waveshed.io/downloads?prv"


def test_helper_rejects_relative_paths():
    with pytest.raises(ValueError):
        site_links.site_page_url("get-key")
    with pytest.raises(ValueError):
        site_links.page_url("get-key")


def test_get_api_key_button_opens_the_resolved_page():
    # The static constant stays the preview link (messages, help); the button
    # opens the resolved URL so it is clean once the site is public.
    assert api_key.GET_API_KEY_URL == "https://waveshed.io/get-key?prv"
    assert api_key.GET_API_KEY_URL == site_links.GET_KEY_URL
    src = (PKG / "gui" / "settings_dialog.py").read_text(encoding="utf-8")
    assert 'QPushButton("Get API Key")' in src
    assert "QUrl(api_key.get_api_key_page_url())" in src
    assert "QUrl(api_key.GET_API_KEY_URL)" not in src


def test_missing_key_message_points_at_key_page():
    ok, msg, _ = api_key.validate_api_key("")
    assert not ok
    assert site_links.GET_KEY_URL in msg


def test_help_links_key_page_through_helper():
    from waveshed.help.content import TOPICS

    install = next(t for t in TOPICS if t.anchor == "install")
    assert f'href="{site_links.GET_KEY_URL}"' in install.body
    assert "{GET_KEY_URL}" not in install.body


# Gated pages must never be hardcoded outside site_links.py, or the gate
# handling would miss them.
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


# --- the gate probe --------------------------------------------------------

class _Response(io.BytesIO):
    def __init__(self, status: int):
        super().__init__(b"<html>")
        self.status = status


class _Probe:
    """A stand-in for ``site_links._open`` that records every call."""

    def __init__(self, outcome):
        self.outcome = outcome  # int status, or an exception instance
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request.full_url, timeout))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return _Response(self.outcome)


def _not_found():
    return urllib.error.HTTPError("https://waveshed.io/get-key", 404,
                                  "Not Found", {}, None)


def test_gate_on_keeps_the_preview_flag(monkeypatch):
    probe = _Probe(_not_found())
    monkeypatch.setattr(site_links, "_open", probe)
    assert site_links.page_url("/get-key") == "https://waveshed.io/get-key?prv"
    assert site_links.page_url("/downloads") == "https://waveshed.io/downloads?prv"
    assert api_key.get_api_key_page_url() == "https://waveshed.io/get-key?prv"


def test_gate_lifted_opens_the_plain_pages(monkeypatch):
    probe = _Probe(200)
    monkeypatch.setattr(site_links, "_open", probe)
    assert site_links.page_url("/get-key") == "https://waveshed.io/get-key"
    assert site_links.page_url("/downloads") == "https://waveshed.io/downloads"
    assert api_key.get_api_key_page_url() == "https://waveshed.io/get-key"
    # Static text keeps the flag: it is harmless on a public page.
    assert site_links.GET_KEY_URL.endswith("?prv")


@pytest.mark.parametrize("failure", [
    urllib.error.URLError("offline"),
    TimeoutError("timed out"),
    OSError("reset"),
])
def test_no_answer_means_the_preview_url(monkeypatch, failure):
    monkeypatch.setattr(site_links, "_open", _Probe(failure))
    assert site_links.page_url("/get-key") == "https://waveshed.io/get-key?prv"


def test_the_probe_asks_get_key_once_per_session(monkeypatch):
    probe = _Probe(200)
    monkeypatch.setattr(site_links, "_open", probe)
    for _ in range(3):
        site_links.page_url("/get-key")
        site_links.page_url("/downloads")
    assert probe.calls == [("https://waveshed.io/get-key",
                            site_links.GATE_PROBE_TIMEOUT_S)]
    assert site_links.GATE_PROBE_TIMEOUT_S <= 3.0  # a button click waits for it

    site_links.reset_gate()
    probe.outcome = _not_found()
    assert site_links.page_url("/get-key") == "https://waveshed.io/get-key?prv"
    assert len(probe.calls) == 2


def test_redirects_and_server_errors_are_not_a_lifted_gate(monkeypatch):
    monkeypatch.setattr(site_links, "_open", _Probe(503))
    assert site_links.page_url("/get-key") == "https://waveshed.io/get-key?prv"
