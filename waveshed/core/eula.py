"""The Aether engine EULA as the plugin presents it.

The canonical, governing EULA text lives in ONE place: the website
(``AETHER_Web/web/static/legal/aether-engine-eula.md``, served at
:data:`EULA_URL` and named by the release manifest's ``eula_url``). The plugin
never ships its own copy of the full text — the download consent dialog fetches
and shows the canonical document.

What the plugin does own is the short, highlighted *key points* summary shown
above the full text, so the essentials are visible even when the EULA cannot be
fetched. It mirrors the "Key points" section of the canonical EULA for
:data:`SUMMARY_EULA_VERSION`; when the fetched EULA carries a different version
the dialog says so, and the full text governs either way. Bump
:data:`SUMMARY_EULA_VERSION` (and :data:`KEY_POINTS`) together with the
``**Version:**`` line of the canonical EULA.

This module never imports GUI code.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

from qgis.core import Qgis, QgsMessageLog, QgsSettings

TAG = "Waveshed"

#: Where the canonical EULA is published (also the manifest's ``eula_url``).
EULA_URL = "https://waveshed.io/legal/aether-engine-eula.md"

#: Contact route for commercial / governmental / organisational permission —
#: the address the website and the EULA itself use.
CONTACT_EMAIL = "info@waveshed.io"

#: EULA version the :data:`KEY_POINTS` summary was written for. Must equal the
#: ``**Version:**`` token of the canonical EULA.
SUMMARY_EULA_VERSION = "2026-09-23"

#: Settings key recording which EULA version the user last accepted. Consent is
#: still asked on every download; this is the record of what was accepted.
ACCEPTED_VERSION_KEY = "waveshed/engine_eula_accepted_version"

NOT_EXHAUSTIVE = (
    "This summary is not exhaustive. The full EULA text below governs; where "
    "this summary and the full text differ, the full text applies."
)

KEY_POINTS: Tuple[str, ...] = (
    "Non-commercial use only: private/personal, hobby, and education or "
    "research by individuals.",
    "Commercial, governmental and organisational use (companies, public "
    "bodies, NGOs, associations and any other organisation) requires the "
    f"author's prior written permission — ask at {CONTACT_EMAIL}.",
    "Results are outputs of a propagation model: approximations, not "
    "measurements, and no guarantee of accuracy.",
    "Do not rely on results for safety-of-life, regulatory, planning or "
    "financial decisions without independent verification.",
    "No warranty: the engine is pre-release software provided \"as is\".",
    "No liability: to the maximum extent permitted by law, the author accepts "
    "no liability for anything arising from use of the engine or its results.",
    "A valid API key is required and must be kept confidential; no "
    "redistribution and no reverse engineering (except where mandatory law "
    "allows it).",
)

_VERSION_RE = re.compile(r"\*\*Version:\*\*\s*([0-9A-Za-z.\-]+)")


def parse_eula_version(text: str) -> Optional[str]:
    """Return the version token of a EULA document (``**Version:** X``)."""
    m = _VERSION_RE.search(text or "")
    return m.group(1) if m else None


def key_points_html() -> str:
    """The highlighted key-points block for a Qt rich-text label."""
    items = "".join(f"<li>{_escape(p)}</li>" for p in KEY_POINTS)
    return (
        "<b>Key points</b>"
        f"<ul style=\"margin-top:4px; margin-bottom:4px;\">{items}</ul>"
        f"<i>{_escape(NOT_EXHAUSTIVE)}</i>"
    )


def _escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fetch_eula_text(url: str = EULA_URL) -> str:
    """Fetch the canonical EULA text. Safe to call from a worker thread.

    Raises :class:`RuntimeError` on a network error or an empty/undecodable
    body, so the caller can fall back to the link.
    """
    from qgis.core import QgsBlockingNetworkRequest
    from qgis.PyQt.QtCore import QUrl
    from qgis.PyQt.QtNetwork import QNetworkRequest

    request = QgsBlockingNetworkRequest()
    err = request.get(QNetworkRequest(QUrl(url)))
    if err != QgsBlockingNetworkRequest.ErrorCode.NoError:
        raise RuntimeError(
            f"Failed to fetch the EULA from {url}: {request.errorMessage()}"
        )
    try:
        text = bytes(request.reply().content()).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"EULA at {url} is not UTF-8: {exc}") from exc
    if not text.strip():
        raise RuntimeError(f"EULA at {url} is empty")
    return text


def record_acceptance(version: str) -> None:
    """Remember which EULA version the user accepted (audit record only)."""
    QgsSettings().setValue(ACCEPTED_VERSION_KEY, version)
    QgsMessageLog.logMessage(
        f"Aether engine EULA version {version} accepted", TAG,
        Qgis.MessageLevel.Info,
    )


def accepted_version() -> Optional[str]:
    """The EULA version last accepted, or ``None``."""
    v = QgsSettings().value(ACCEPTED_VERSION_KEY, None)
    return str(v) if v else None
