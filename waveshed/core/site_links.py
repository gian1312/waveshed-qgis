"""Links from the plugin to pages on waveshed.io.

The single place that builds a URL to a human-facing waveshed.io page. The
engine download (``/downloads``) and the key generator (``/get-key``) used to
sit behind a preview gate on the site. The gate was lifted on 2026-11-08: both
pages are public, and the site ignores the old preview flag, so plugins that
still append it keep working.

* :func:`site_page_url` builds the plain URL for static text (the help, the
  messages): :data:`PREVIEW_QUERY` is empty.
* :func:`page_url` is what a button opens. While :data:`PREVIEW_QUERY` is empty
  it returns the plain URL without any network call. Only if a preview query
  is set again (a future gate) does it ask the site ONCE per QGIS session
  whether ``/get-key`` answers without the flag, and returns the plain URL if
  so, else the preview URL. That probe machinery stays here, dormant.

Machine endpoints are deliberately *not* built here and never carry a preview
flag: the release manifest (``binary_manager.MANIFEST_URL``) and the EULA text
(``eula.EULA_URL``) are public files, not gated pages.

Pure standard library, no Qt/QGIS imports: ``help/content.py`` (which must stay
importable without QGIS) uses it too.
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from typing import Optional

#: Site root.
SITE_URL = "https://waveshed.io"

#: Query a gated page needs. Empty since the gate was lifted (2026-11-08):
#: static links are plain and :func:`page_url` does not probe. Set it (e.g.
#: to the former value, the ``prv`` flag) only for a future preview gate; it
#: is appended by :func:`site_page_url` and wakes the probe in
#: :func:`page_url`.
PREVIEW_QUERY = ""

#: The page whose plain answer tells a gate's state: 404 while gated, 2xx
#: once public. Only asked while :data:`PREVIEW_QUERY` is set.
GATE_PROBE_PATH = "/get-key"

#: Seconds the one probe per session may take (only while a preview query is
#: set); after that the preview URL is used.
GATE_PROBE_TIMEOUT_S = 2.0


def site_page_url(path: str) -> str:
    """Return the URL of the waveshed.io page at *path* for static text.

    *path* must start with ``/``. :data:`PREVIEW_QUERY` is appended; it is
    empty since the gate was lifted, so this is the plain URL. For the URL a
    button should open see :func:`page_url`.
    """
    if not path.startswith("/"):
        raise ValueError(f"site path must start with '/': {path!r}")
    return f"{SITE_URL}{path}{PREVIEW_QUERY}"


def page_url(path: str) -> str:
    """The URL a button opens for the page at *path*.

    With an empty :data:`PREVIEW_QUERY` (since 2026-11-08) the plain URL,
    without a network call. While a preview query is set: the plain page once
    the site serves it publicly, the preview URL until then — or when the
    site cannot be asked."""
    if not path.startswith("/"):
        raise ValueError(f"site path must start with '/': {path!r}")
    if not PREVIEW_QUERY or gate_lifted():
        return f"{SITE_URL}{path}"
    return site_page_url(path)


_gate_lock = threading.Lock()
_gate_lifted: Optional[bool] = None


def gate_lifted() -> bool:
    """Whether the site serves the gated pages without the preview flag.
    Probed once per session (see :data:`GATE_PROBE_PATH`); False when the
    probe gets a 404, no answer, or any error."""
    global _gate_lifted
    with _gate_lock:
        if _gate_lifted is None:
            _gate_lifted = _probe_gate()
        return _gate_lifted


def reset_gate() -> None:
    """Forget the probe's answer, so the next :func:`page_url` asks again."""
    global _gate_lifted
    with _gate_lock:
        _gate_lifted = None


def _probe_gate() -> bool:
    request = urllib.request.Request(
        f"{SITE_URL}{GATE_PROBE_PATH}",
        headers={"User-Agent": "waveshed-qgis-plugin"})
    try:
        with _open(request, GATE_PROBE_TIMEOUT_S) as response:
            return 200 <= int(response.status) < 300
    except urllib.error.HTTPError:  # 404 while the gate is on
        return False
    except Exception:  # noqa: BLE001 — offline, timeout, odd answer: preview works either way
        return False


def _open(request: urllib.request.Request, timeout: float):
    """``urlopen`` behind one seam the tests replace."""
    return urllib.request.urlopen(request, timeout=timeout)  # nosec B310 — fixed https URL


#: Where users get an access (API) key: the key generator page (static text;
#: buttons open ``page_url("/get-key")``).
GET_KEY_URL = site_page_url("/get-key")

#: The engine downloads page (engine details, EULA, manual downloads).
DOWNLOADS_URL = site_page_url("/downloads")
