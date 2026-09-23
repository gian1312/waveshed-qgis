"""Built-in help: a topic list beside the rendered documentation.

The text itself lives in :mod:`waveshed.help` (plain HTML fragments, no Qt), so
this module is only the reader: it renders the one document, lets the topic
list and internal links jump around inside it, and filters the list from a
search box.

Rendering the *whole* document rather than one topic at a time is deliberate —
it keeps cross-references between topics working as ordinary anchors, and it
lets the user scroll from one subject into the next the way a manual reads.
"""

from __future__ import annotations

from typing import List, Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..help import DEFAULT_ANCHOR, TOPICS, build_html, find_topic, matches

#: Styling for the rendered document. Qt's rich-text engine supports a small
#: CSS subset, so this stays to element selectors and simple properties.
_STYLESHEET = """
h1 { font-size: 17pt; }
h2 { font-size: 13pt; margin-top: 18px; }
h3 { font-size: 11pt; margin-top: 14px; }
p, li, td, th { font-size: 10pt; }
code, pre { font-family: monospace; }
pre { background-color: #f2f2f2; padding: 6px; }
table { margin-top: 6px; margin-bottom: 6px; }
th { background-color: #f2f2f2; text-align: left; }
p.totop { font-size: 8pt; margin-bottom: 20px; }
"""


class HelpTab(QWidget):
    """Topic list + rendered help document.

    Public API:
      * :meth:`show_topic` — jump to a topic by its anchor.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        #: Anchor asked for while the widget was not yet visible; re-applied
        #: on the next show, because scrolling a document that has never been
        #: laid out does nothing.
        self._pending_anchor: str = DEFAULT_ANCHOR
        #: The document is ~65 KB of HTML and laying it out costs Qt about
        #: 45 ms, which every user would pay when the analysis dialog opens
        #: even though most never look at Help. Built on first sight instead.
        self._document_ready = False
        self._build_ui()
        self._connect_signals()
        self._populate_topics()
        self._select_anchor(DEFAULT_ANCHOR)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Search:"))
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText(
            "Filter topics by title or content, e.g. “AMSL”, "
            "“API key”, “resolution”")
        self.search_box.setClearButtonEnabled(True)
        search_row.addWidget(self.search_box, 1)
        layout.addLayout(search_row)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)

        self.topic_list = QListWidget()
        self.topic_list.setMinimumWidth(190)
        self.splitter.addWidget(self.topic_list)

        self.browser = QTextBrowser()
        # http(s) links go to the user's browser; everything else (our own
        # "#anchor" targets) is handled in _on_anchor_clicked, which scrolls
        # inside the document. setOpenLinks(False) is what keeps QTextBrowser
        # from trying to *load* "#anchor" as a document of its own — with no
        # source set, that blanks the page.
        self.browser.setOpenExternalLinks(True)
        self.browser.setOpenLinks(False)
        self.browser.document().setDefaultStyleSheet(_STYLESHEET)
        self.splitter.addWidget(self.browser)

        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        layout.addWidget(self.splitter, 1)

    def _connect_signals(self) -> None:
        self.search_box.textChanged.connect(self._on_search)
        self.topic_list.currentRowChanged.connect(self._on_topic_row_changed)
        self.browser.anchorClicked.connect(self._on_anchor_clicked)

    # ------------------------------------------------------------------
    # Topic list
    # ------------------------------------------------------------------

    def _populate_topics(self, query: str = "") -> None:
        """Fill the list with the topics matching *query* (all when empty)."""
        blocked = self.topic_list.blockSignals(True)
        self.topic_list.clear()
        for topic in TOPICS:
            if not matches(topic, query):
                continue
            item = QListWidgetItem(topic.title)
            item.setData(Qt.ItemDataRole.UserRole, topic.anchor)
            item.setToolTip(topic.title)
            self.topic_list.addItem(item)
        self.topic_list.blockSignals(blocked)

    def _listed_anchors(self) -> List[str]:
        anchors: List[str] = []
        for row in range(self.topic_list.count()):
            item = self.topic_list.item(row)
            if item is not None:
                anchors.append(str(item.data(Qt.ItemDataRole.UserRole) or ""))
        return anchors

    def _select_anchor(self, anchor: str) -> None:
        """Highlight *anchor*'s row without re-triggering navigation."""
        try:
            row = self._listed_anchors().index(anchor)
        except ValueError:
            return
        blocked = self.topic_list.blockSignals(True)
        self.topic_list.setCurrentRow(row)
        self.topic_list.blockSignals(blocked)

    # ------------------------------------------------------------------
    # The document
    # ------------------------------------------------------------------

    def _ensure_document(self) -> None:
        """Render the help document once, the first time it is needed."""
        if self._document_ready:
            return
        self.browser.setHtml(build_html())
        self._document_ready = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def show_topic(self, anchor: str) -> None:
        """Scroll the document to *anchor* and select it in the topic list.

        An unknown (or empty) anchor falls back to the first topic, so a
        caller may pass a stale name without breaking the tab. A filtered-out
        topic clears the search box first — otherwise the list would not be
        able to show the topic the document just jumped to.
        """
        topic = find_topic(anchor) or find_topic(DEFAULT_ANCHOR)
        if topic is None:  # empty content package — nothing to show
            return
        if topic.anchor not in self._listed_anchors():
            self.search_box.clear()  # triggers _on_search -> repopulate
        self._select_anchor(topic.anchor)
        self._pending_anchor = topic.anchor
        self._ensure_document()
        self.browser.scrollToAnchor(topic.anchor)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_search(self, text: str) -> None:
        self._populate_topics(text)
        anchors = self._listed_anchors()
        if not anchors:
            return
        # Follow the filter: jump to the first match so the document and the
        # list never disagree about what is being read.
        if self._pending_anchor in anchors:
            self._select_anchor(self._pending_anchor)
        else:
            self._select_anchor(anchors[0])
            self._pending_anchor = anchors[0]
            self._ensure_document()
            self.browser.scrollToAnchor(anchors[0])

    def _on_topic_row_changed(self, row: int) -> None:
        if row < 0:
            return
        item = self.topic_list.item(row)
        if item is None:
            return
        anchor = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if anchor:
            self._pending_anchor = anchor
            self._ensure_document()
            self.browser.scrollToAnchor(anchor)

    def _on_anchor_clicked(self, url) -> None:
        """Follow a link: external in the browser, internal by scrolling."""
        scheme = ""
        fragment = ""
        try:
            scheme = (url.scheme() or "").lower()
            fragment = url.fragment() or ""
        except AttributeError:  # a plain string, or a stubbed URL in tests
            text = str(url)
            fragment = text.split("#", 1)[1] if "#" in text else ""
        if scheme in ("http", "https", "mailto"):
            QDesktopServices.openUrl(url)
            return
        if fragment:
            self.show_topic(fragment)

    # ------------------------------------------------------------------
    # Qt overrides
    # ------------------------------------------------------------------

    def showEvent(self, event) -> None:
        """Re-apply the pending anchor once the document has a layout.

        ``scrollToAnchor`` on a document that has never been shown is a no-op,
        so opening the dialog straight onto a topic (the "Waveshed Help" menu
        entry, or the "?" button) would otherwise land at the top.
        """
        super().showEvent(event)
        self._ensure_document()
        if self._pending_anchor:
            self.browser.scrollToAnchor(self._pending_anchor)
