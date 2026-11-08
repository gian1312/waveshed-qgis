"""Links from the plugin to pages on waveshed.io.

The single place that builds a URL to a human-facing waveshed.io page. The
engine download (``/downloads``) and the key generator (``/get-key``) sit
behind a preview gate on the site until the public launch: without ``?prv``
the downloads page shows only a "coming soon" overlay and ``/get-key``
answers 404. The plugin works on both sides of that launch without a new
release:

* :func:`site_page_url` builds the preview URL (``…?prv``). Static text — the
  help, the messages — uses it; the flag is harmless once the gate is gone.
* :func:`page_url` is what a button opens. It asks the site ONCE per QGIS
  session whether ``/get-key`` answers without the flag (the gate is lifted)
  and then returns the plain URL, else the preview URL. No answer at all
  (offline, a timeout) also means the preview URL, which works either way.

Machine endpoints are deliberately *not* built here and never carry ``?prv``:
the release manifest (``binary_manager.MANIFEST_URL``) and the EULA text
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

#: The preview flag the gated pages need until the public launch. Appended by
#: :func:`site_page_url`; :func:`page_url` drops it once the site answers
#: without it. Harmless on a page that is already public. May be set to ""
#: in a release after the launch to tidy the static links; nothing depends
#: on it.
PREVIEW_QUERY = "?prv"

#: The page whose plain answer tells the gate's state: 404 while gated, 2xx
#: once public. (``/downloads`` answers 200 either way, with an overlay.)
GATE_PROBE_PATH = "/get-key"

#: Seconds the one probe per session may take; after that the preview URL
#: is used.
GATE_PROBE_TIMEOUT_S = 2.0


def site_page_url(path: str) -> str:
    """Return the preview URL of the waveshed.io page at *path*.

    *path* must start with ``/``. The preview flag is appended so the page
    renders its content while the site is gated; it changes nothing once the
    page is public. For the URL a button should open see :func:`page_url`.
    """
    if not path.startswith("/"):
        raise ValueError(f"site path must start with '/': {path!r}")
    return f"{SITE_URL}{path}{PREVIEW_QUERY}"


def page_url(path: str) -> str:
    """The URL to open for the gated page at *path*: the plain page once the
    site serves the pages publicly, the preview URL until then — or when the
    site cannot be asked."""
    if not path.startswith("/"):
        raise ValueError(f"site path must start with '/': {path!r}")
    if gate_lifted():
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


#: Where users get an access (API) key: the key generator page (preview URL,
#: for static text; buttons open ``page_url("/get-key")``).
GET_KEY_URL = site_page_url("/get-key")

#: The engine downloads page (engine details, EULA, manual downloads).
DOWNLOADS_URL = site_page_url("/downloads")
