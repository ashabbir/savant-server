"""TDD tests for unified tab component — shared across all pages."""

import os
import re
import unittest

SHARED_CSS = os.path.join(os.path.dirname(__file__), "..", "static", "css", "shared.css")
TABS_JS = os.path.join(os.path.dirname(__file__), "..", "static", "js", "tabs.js")
INDEX = os.path.join(os.path.dirname(__file__), "..", "templates", "index.html")
DETAIL = os.path.join(os.path.dirname(__file__), "..", "templates", "detail.html")


def _read(path):
    with open(path) as f:
        return f.read()


class TestTabCSS(unittest.TestCase):
    """Shared CSS must define the unified tab classes."""

    @classmethod
    def setUpClass(cls):
        cls.css = _read(SHARED_CSS)

    def test_has_savant_tabs_container(self):
        self.assertIn(".savant-tabs", self.css)

    def test_has_savant_tab_base(self):
        self.assertIn(".savant-tab", self.css)

    def test_has_savant_tab_active(self):
        self.assertIn(".savant-tab.active", self.css)

    def test_has_savant_tab_hover(self):
        self.assertIn(".savant-tab:hover", self.css)

    def test_has_savant_subtabs_container(self):
        self.assertIn(".savant-subtabs", self.css)

    def test_has_savant_subtab_base(self):
        self.assertIn(".savant-subtab", self.css)

    def test_has_savant_subtab_active(self):
        self.assertIn(".savant-subtab.active", self.css)

    def test_tab_uses_css_variables(self):
        """Tab styles should use theme variables, not hardcoded colors."""
        # Find the .savant-tab rule block
        match = re.search(r'\.savant-tab\s*\{([^}]+)\}', self.css)
        self.assertIsNotNone(match, ".savant-tab rule not found")
        body = match.group(1)
        self.assertIn("var(--", body, "savant-tab should use CSS variables")

    def test_tab_panel_class(self):
        self.assertIn(".savant-tab-panel", self.css)

    def test_tab_badge_class(self):
        self.assertIn(".savant-tab .tab-badge", self.css)

    def test_tab_has_consistent_font(self):
        """Both tab tiers should use the same font family."""
        tab_match = re.search(r'\.savant-tab\s*\{([^}]+)\}', self.css)
        subtab_match = re.search(r'\.savant-subtab\s*\{([^}]+)\}', self.css)
        self.assertIsNotNone(tab_match)
        self.assertIsNotNone(subtab_match)
        # Both should reference font-mono
        self.assertIn("font-mono", tab_match.group(1))
        self.assertIn("font-mono", subtab_match.group(1))


class TestTabJS(unittest.TestCase):
    """Shared JS must export a switchTab function."""

    @classmethod
    def setUpClass(cls):
        cls.js = _read(TABS_JS)

    def test_file_exists(self):
        self.assertTrue(os.path.isfile(TABS_JS))

    def test_exports_savantSwitchTab(self):
        self.assertIn("function savantSwitchTab", self.js)

    def test_accepts_container_param(self):
        """Should work with a container selector for scoped tab switching."""
        self.assertIn("container", self.js)

    def test_toggles_active_class(self):
        self.assertIn("classList", self.js)
        self.assertIn("active", self.js)

    def test_valid_js_syntax(self):
        import subprocess
        result = subprocess.run(
            ["node", "--check", TABS_JS],
            capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, f"JS syntax error: {result.stderr}")


class TestDetailUsesSharedTabs(unittest.TestCase):
    """detail.html should use the shared tab component."""

    @classmethod
    def setUpClass(cls):
        cls.src = _read(DETAIL)

    def test_imports_tabs_js(self):
        self.assertIn("tabs.js", self.src)

    def test_uses_savant_tabs_class(self):
        self.assertIn("savant-tabs", self.src)

    def test_uses_savant_tab_class(self):
        self.assertIn("savant-tab", self.src)

    def test_no_inline_tab_btn_css(self):
        """detail.html should NOT define its own .tab-btn CSS rule."""
        # Check there's no CSS definition for .tab-btn
        self.assertNotIn(".tab-btn {", self.src,
                         "detail.html should not define .tab-btn CSS — use shared .savant-tab")

    def test_no_inline_tracker_tab_css(self):
        self.assertNotIn(".tracker-tab {", self.src,
                         "detail.html should not define .tracker-tab CSS — use shared .savant-subtab")


class TestIndexUsesSharedTabs(unittest.TestCase):
    """index.html should use the shared tab component for sub-tabs."""

    @classmethod
    def setUpClass(cls):
        cls.src = _read(INDEX)

    def test_imports_tabs_js(self):
        self.assertIn("tabs.js", self.src)

    def test_uses_savant_subtab_for_providers(self):
        """Provider tabs should use .savant-subtab class."""
        self.assertIn("savant-subtab", self.src)

    def test_no_inline_ws_sub_tab_css(self):
        self.assertNotIn(".ws-sub-tab {", self.src,
                         "index.html should not define .ws-sub-tab CSS — use shared .savant-subtab")

    def test_no_inline_tutorial_tab_css(self):
        self.assertNotIn(".tutorial-tab {", self.src,
                         "index.html should not define .tutorial-tab CSS — use shared .savant-subtab")

    def test_no_inline_ctx_tab_css(self):
        self.assertNotIn(".ctx-tab {", self.src,
                         "index.html should not define .ctx-tab CSS — use shared .savant-subtab")


if __name__ == "__main__":
    unittest.main()
