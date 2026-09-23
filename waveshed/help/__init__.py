"""Built-in user documentation for the Waveshed plugin.

The content lives in :mod:`waveshed.help.content` as plain HTML fragments, one
per topic, and this module turns them into the single document the Help tab
renders plus the topic index beside it.

Deliberately GUI-free: nothing here imports Qt or QGIS, so the whole help text
is unit-testable (``tests/test_help_content.py`` checks that every topic has an
anchor, that every internal link resolves, and that the document names the
controls the code really builds).
"""

from __future__ import annotations

from typing import List, Optional

from .content import TOPICS, Topic

__all__ = [
    "Topic",
    "TOPICS",
    "DEFAULT_ANCHOR",
    "anchors",
    "find_topic",
    "titles",
    "section_html",
    "build_html",
    "matches",
    "search",
]

#: Topic shown when the Help tab is opened without one.
DEFAULT_ANCHOR = "overview"


def anchors() -> List[str]:
    """Every topic anchor, in document order."""
    return [topic.anchor for topic in TOPICS]


def titles() -> List[str]:
    """Every topic title, in document order."""
    return [topic.title for topic in TOPICS]


def find_topic(anchor: str) -> Optional[Topic]:
    """The topic with *anchor*, or ``None``.

    Comparison is case-insensitive and tolerates a leading ``#`` so a caller
    can pass a link target straight through.
    """
    key = (anchor or "").strip().lstrip("#").lower()
    if not key:
        return None
    for topic in TOPICS:
        if topic.anchor.lower() == key:
            return topic
    return None


def section_html(topic: Topic) -> str:
    """One topic rendered as a section, heading and named anchor included."""
    return (
        f'<h2><a name="{topic.anchor}"></a>{topic.title}</h2>\n'
        f"{topic.body.strip()}\n"
        f'<p class="totop"><a href="#top">Back to contents</a></p>'
    )


def _contents_html() -> str:
    items = "\n".join(
        f'<li><a href="#{t.anchor}">{t.title}</a></li>' for t in TOPICS
    )
    return (
        '<h1><a name="top"></a>Waveshed Help</h1>\n'
        "<p>RF propagation analysis in QGIS, powered by the Aether engine. "
        "This help describes what the plugin does today; every control is "
        "named exactly as it appears in the dialog.</p>\n"
        f"<h2>Contents</h2>\n<ul>\n{items}\n</ul>"
    )


def build_html() -> str:
    """The complete help document (a single HTML body, no ``<head>``)."""
    parts = [_contents_html()]
    parts.extend(section_html(topic) for topic in TOPICS)
    return "\n\n".join(parts)


def _plain_text(html: str) -> str:
    """Crude tag-stripper, good enough for substring search over a body."""
    out: List[str] = []
    depth = 0
    for char in html:
        if char == "<":
            depth += 1
        elif char == ">":
            if depth:
                depth -= 1
        elif depth == 0:
            out.append(char)
    return "".join(out)


def matches(topic: Topic, query: str) -> bool:
    """True if *query* occurs in the topic's title, keywords or body text.

    Plain case-insensitive substring matching — the search box filters the
    topic list, it is not a search engine.
    """
    needle = (query or "").strip().lower()
    if not needle:
        return True
    haystack = " ".join(
        [topic.title, topic.anchor, " ".join(topic.keywords),
         _plain_text(topic.body)]
    ).lower()
    return needle in haystack


def search(query: str) -> List[Topic]:
    """Topics matching *query*, in document order (all of them when empty)."""
    return [topic for topic in TOPICS if matches(topic, query)]
