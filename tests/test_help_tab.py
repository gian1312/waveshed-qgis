"""The Help tab and the ways in which it is opened.

The widget itself cannot be constructed under the Qt stubs in ``conftest``
(they are inert classes), so this file does what the other GUI tests do:
imports the module — which catches a typo or a bad import straight away — and
asserts on the source for the wiring that must hold.
"""

from __future__ import annotations

import re
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
GUI = PLUGIN_ROOT / "waveshed" / "gui"

HELP_TAB_SRC = (GUI / "help_tab.py").read_text(encoding="utf-8")
MAIN_DIALOG_SRC = (GUI / "main_dialog.py").read_text(encoding="utf-8")
PLUGIN_SRC = (PLUGIN_ROOT / "waveshed" / "plugin.py").read_text(encoding="utf-8")


class TestHelpTabModule:
    def test_module_imports(self):
        from waveshed.gui import help_tab

        assert hasattr(help_tab, "HelpTab")

    def test_show_topic_is_public_api(self):
        from waveshed.gui.help_tab import HelpTab

        assert callable(getattr(HelpTab, "show_topic", None))

    def test_renders_with_a_text_browser_and_a_topic_list(self):
        assert "QTextBrowser" in HELP_TAB_SRC
        assert "QListWidget" in HELP_TAB_SRC
        assert "QLineEdit" in HELP_TAB_SRC  # the search box

    def test_links_and_anchors_are_wired(self):
        assert "setOpenExternalLinks(True)" in HELP_TAB_SRC
        assert "scrollToAnchor" in HELP_TAB_SRC
        assert "anchorClicked" in HELP_TAB_SRC

    def test_content_comes_from_the_help_package(self):
        assert "from ..help import" in HELP_TAB_SRC
        assert "build_html()" in HELP_TAB_SRC

    def test_search_uses_the_shared_matcher(self):
        """The filter must be the one the tests cover, not a second copy."""
        assert "matches(topic" in HELP_TAB_SRC


class TestMainDialogIntegration:
    def test_help_is_the_last_tab(self):
        tabs = re.findall(r'self\.tabs\.addTab\([^,]+,\s*"([^"]+)"\)',
                          MAIN_DIALOG_SRC)
        assert tabs, "no tabs found — the scraper is broken"
        assert tabs[-1] == "Help", f"tab order is {tabs}"
        assert tabs.index("Settings") == tabs.index("Help") - 1, (
            f"Help must come directly after Settings; got {tabs}")

    def test_show_help_exists_and_selects_the_tab(self):
        from waveshed.gui.main_dialog import AetherMainDialog

        assert callable(getattr(AetherMainDialog, "show_help", None))
        body = MAIN_DIALOG_SRC.split("def show_help", 1)[1].split("def ", 1)[0]
        assert "setCurrentWidget(self.help_tab)" in body
        assert "show_topic(anchor)" in body

    def test_show_help_anchor_is_optional(self):
        assert re.search(r"def show_help\(self, anchor: str = \"\"\)",
                         MAIN_DIALOG_SRC)

    def test_mode_help_button_opens_the_models_topic(self):
        assert "QToolButton" in MAIN_DIALOG_SRC
        assert 'self.show_help("models")' in MAIN_DIALOG_SRC

    def test_the_models_anchor_the_button_uses_exists(self):
        from waveshed import help as help_pkg

        anchors = set(re.findall(r'show_help\("([^"]*)"\)', MAIN_DIALOG_SRC))
        anchors |= set(re.findall(r'_open_help\("([^"]*)"\)', PLUGIN_SRC))
        for anchor in anchors:
            if not anchor:
                continue
            assert help_pkg.find_topic(anchor) is not None, (
                f'show_help("{anchor}") points at a topic that does not exist')


class TestPluginMenu:
    def test_menu_action_exists(self):
        assert '"Waveshed Help"' in PLUGIN_SRC
        assert "self._open_help" in PLUGIN_SRC
        assert "addPluginToMenu(self.menu_name, self.action_help)" in PLUGIN_SRC

    def test_action_is_unloaded_with_the_others(self):
        """`unload` iterates self.actions, so the new action must be in it."""
        assert "self.actions.append(self.action_help)" in PLUGIN_SRC

    def test_open_help_reuses_the_main_dialog(self):
        body = PLUGIN_SRC.split("def _open_help", 1)[1].split("\n    def ", 1)[0]
        assert "self._open_main_dialog()" in body
        assert "show_help" in body

    def test_triggered_does_not_leak_the_checked_flag(self):
        """QAction.triggered passes a bool; it must not become the anchor."""
        assert "self.action_help.triggered.connect(lambda: self._open_help())" \
            in PLUGIN_SRC
