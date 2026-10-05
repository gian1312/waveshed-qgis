"""Links from the plugin to pages on waveshed.io.

The single place that builds a URL to a human-facing waveshed.io page. The
engine download (``/downloads``) and the key generator (``/get-key``) are a
private preview on the site: without ``?prv`` the downloads page shows only a
"coming soon" overlay and ``/get-key`` answers 404. Every link the plugin opens
to such a page therefore goes through :func:`site_page_url`, which appends
:data:`PREVIEW_QUERY`.

Machine endpoints are deliberately *not* built here and never carry ``?prv``:
the release manifest (``binary_manager.MANIFEST_URL``) and the EULA text
(``eula.EULA_URL``) are public files, not gated pages.

Pure standard library, no Qt/QGIS imports: ``help/content.py`` (which must stay
importable without QGIS) uses it too.
"""

from __future__ import annotations

#: Site root.
SITE_URL = "https://waveshed.io"

# ---------------------------------------------------------------------------
# TODO(release): remove ?prv before publication.
#
# Set PREVIEW_QUERY = "" once /downloads and /get-key are public. `package.py`
# prints a loud warning for every packaged file that still contains "?prv".
# Checklists: QGIS_Plugin/TODO.md and AETHER_Web/TODO.md ("remove ?prv").
# ---------------------------------------------------------------------------
PREVIEW_QUERY = "?prv"


def site_page_url(path: str) -> str:
    """Return the absolute URL of the waveshed.io page at *path*.

    *path* must start with ``/``. The preview flag is appended so the page
    renders the preview content rather than its public placeholder.
    """
    if not path.startswith("/"):
        raise ValueError(f"site path must start with '/': {path!r}")
    return f"{SITE_URL}{path}{PREVIEW_QUERY}"


#: Where users get an access (API) key: the key generator page.
GET_KEY_URL = site_page_url("/get-key")

#: The engine downloads page (engine details, EULA, manual downloads).
DOWNLOADS_URL = site_page_url("/downloads")
