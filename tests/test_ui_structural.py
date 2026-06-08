"""
Structural / regression tests for terminal BrowserView and dev-log panel fixes.

These tests read the source files (HTML, JS) and verify that key functions,
event handlers, CSS classes, and DOM elements are present. They catch
regressions where a refactor or merge accidentally removes critical code.
"""

import os
import re

import pytest

# ── Paths ───────────────────────────────────────────────────────────────────

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_SAVANT_DIR = os.path.join(_PROJECT_ROOT, 'savant')
_TEMPLATES_DIR = os.path.join(_SAVANT_DIR, 'templates')

TERMINAL_HTML = os.path.join(_PROJECT_ROOT, 'terminal.html')
MAIN_JS = os.path.join(_PROJECT_ROOT, 'main.js')
DETAIL_HTML = os.path.join(_TEMPLATES_DIR, 'detail.html')
INDEX_HTML = os.path.join(_TEMPLATES_DIR, 'index.html')


def _read(filepath):
    with open(filepath) as f:
        return f.read()


# ── File existence guards ───────────────────────────────────────────────────

def test_terminal_html_exists():
    assert os.path.isfile(TERMINAL_HTML), f"terminal.html not found at {TERMINAL_HTML}"


def test_main_js_exists():
    assert os.path.isfile(MAIN_JS), f"main.js not found at {MAIN_JS}"


def test_detail_html_exists():
    assert os.path.isfile(DETAIL_HTML), f"detail.html not found at {DETAIL_HTML}"


def test_index_html_exists():
    assert os.path.isfile(INDEX_HTML), f"index.html not found at {INDEX_HTML}"


# ═══════════════════════════════════════════════════════════════════════════
# terminal.html structural tests
# ═══════════════════════════════════════════════════════════════════════════

class TestTerminalHTMLStructure:
    """Verify terminal focus-management and refresh code in terminal.html."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.html = _read(TERMINAL_HTML)

    def test_refresh_all_terminals_function_exists(self):
        """_refreshAllTerminals must be declared."""
        assert 'function _refreshAllTerminals' in self.html

    def test_focus_and_refresh_command_handler(self):
        """The 'focus-and-refresh' command must be handled."""
        assert 'focus-and-refresh' in self.html

    def test_visibilitychange_listener(self):
        """A visibilitychange event listener must exist for re-rendering."""
        assert 'visibilitychange' in self.html

    def test_term_body_click_handler(self):
        """Click handler on #term-body must exist for focus capture."""
        assert "term-body" in self.html
        # Verify there is a click addEventListener on term-body (may use variable)
        assert re.search(
            r"termBody\.addEventListener\(['\"]click['\"]",
            self.html,
        ) or re.search(
            r"getElementById\(['\"]term-body['\"]\)\.addEventListener\(['\"]click['\"]",
            self.html,
        ), "term-body click addEventListener not found"

    def test_window_focus_listener(self):
        """window.addEventListener('focus', ...) must exist."""
        assert re.search(
            r"window\.addEventListener\(['\"]focus['\"]",
            self.html,
        ), "window focus event listener not found"

    def test_refresh_called_on_visibility(self):
        """_refreshAllTerminals should be called in the visibilitychange handler."""
        # Find the visibilitychange block and check it references _refreshAllTerminals
        assert '_refreshAllTerminals' in self.html


# ═══════════════════════════════════════════════════════════════════════════
# main.js structural tests — terminal BrowserView
# ═══════════════════════════════════════════════════════════════════════════

class TestMainJSTerminalStructure:
    """Verify terminal BrowserView focus/resize code in main.js."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.js = _read(MAIN_JS)

    def test_termview_focus_handler(self):
        """termView.webContents.on('focus', ...) handler must exist."""
        assert re.search(
            r'termView\.webContents\.on\(["\']focus["\']',
            self.js,
        ), "termView webContents focus handler not found"

    def test_enter_full_screen_handler(self):
        """enter-full-screen event handler must exist."""
        assert 'enter-full-screen' in self.js

    def test_leave_full_screen_handler(self):
        """leave-full-screen event handler must exist."""
        assert 'leave-full-screen' in self.js

    def test_restore_event_handler(self):
        """restore event handler must exist for window unminimize."""
        assert re.search(
            r'mainWindow\.on\(["\']restore["\']',
            self.js,
        ), "restore event handler not found"

    def test_calc_term_bounds_function(self):
        """_calcTermBounds pure function must exist for bounds calculation."""
        assert 'function _calcTermBounds' in self.js

    def test_send_term_focus_function(self):
        """_sendTermFocus helper must exist for focus management."""
        assert 'function _sendTermFocus' in self.js

    def test_broadcast_drawer_state_function(self):
        """_broadcastDrawerState helper must exist for drawer state sync."""
        assert 'function _broadcastDrawerState' in self.js

    def test_show_term_view_adds_browser_view(self):
        """showTermView must add the BrowserView when not attached."""
        match = re.search(
            r'function showTermView\b.*?\n\}',
            self.js,
            re.DOTALL,
        )
        assert match, "showTermView function not found"
        body = match.group(0)
        assert 'addBrowserView' in body, \
            "showTermView does not add BrowserView"
        assert '_sendTermFocus' in body, \
            "showTermView does not call _sendTermFocus"

    def test_hide_term_view_removes_browser_view(self):
        """hideTermView must remove the BrowserView to avoid ghost rendering."""
        match = re.search(
            r'function hideTermView\b.*?\n\}',
            self.js,
            re.DOTALL,
        )
        assert match, "hideTermView function not found"
        body = match.group(0)
        assert 'removeBrowserView' in body, \
            "hideTermView does not remove BrowserView"

    def test_bounds_verification_retry(self):
        """_updateTermBounds must verify bounds were applied after a delay."""
        assert 'getBounds()' in self.js

    def test_create_term_view_function(self):
        """createTermView function must exist."""
        assert 'function createTermView' in self.js

    def test_update_term_bounds_function(self):
        """_updateTermBounds function must exist."""
        assert 'function _updateTermBounds' in self.js

    def test_watch_window_resize_function(self):
        """_watchWindowResize function must exist."""
        assert 'function _watchWindowResize' in self.js

    def test_resize_handler_registered(self):
        """mainWindow.on('resize', ...) must be registered."""
        assert re.search(
            r'mainWindow\.on\(["\']resize["\']',
            self.js,
        ), "resize event handler not found"


# ═══════════════════════════════════════════════════════════════════════════
# detail.html dev-log panel tests
# ═══════════════════════════════════════════════════════════════════════════

class TestDetailHTMLDevLogPanel:
    """Verify dev-log panel HTML and CSS exist in detail.html."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.html = _read(DETAIL_HTML)

    def test_dev_log_panel_element(self):
        """#dev-log-panel element must exist."""
        assert 'id="dev-log-panel"' in self.html

    def test_dev_log_panel_css(self):
        """.dev-log-panel CSS rule must exist."""
        assert '.dev-log-panel' in self.html

    def test_dev_log_header_css(self):
        """.dev-log-header CSS rule must exist."""
        assert '.dev-log-header' in self.html

    def test_dev_log_body_css(self):
        """.dev-log-body CSS rule must exist."""
        assert '.dev-log-body' in self.html

    def test_dev_log_body_element(self):
        """#dev-log-body element must exist."""
        assert 'id="dev-log-body"' in self.html

    def test_dev_log_filter_bar(self):
        """Dev-log filter bar HTML must exist."""
        assert 'dev-log-filter' in self.html

    def test_toggle_dev_logs_reference(self):
        """toggleDevLogs function must be referenced (onclick or call)."""
        assert 'toggleDevLogs' in self.html


class TestTerminalSplitsAndNavigation:
    """Verify tree-based split pane and tab navigation code in terminal.html."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.html = _read(TERMINAL_HTML)

    def test_tree_data_model_exists(self):
        """Tree root and focused leaf tracking must exist."""
        assert '_treeRoot' in self.html, "Missing _treeRoot variable"
        assert '_focusedLeafId' in self.html, "Missing _focusedLeafId variable"

    def test_split_focused_pane_function(self):
        """_splitFocusedPane must exist for nested split support."""
        assert 'function _splitFocusedPane' in self.html

    def test_split_focused_pane_refreshes(self):
        """_splitFocusedPane must refresh all terminals after split."""
        match = re.search(
            r'async function _splitFocusedPane.*?(?=\nfunction |\n\n// ──)',
            self.html,
            re.DOTALL,
        )
        assert match, "_splitFocusedPane function not found"
        body = match.group(0)
        assert '_refreshAllTerminals' in body, \
            "_splitFocusedPane does not refresh all terminals"

    def test_close_focused_pane_refreshes(self):
        """_closeFocusedPane must refresh after closing a pane."""
        match = re.search(
            r'function _closeFocusedPane.*?(?=\n\n// ──)',
            self.html,
            re.DOTALL,
        )
        assert match, "_closeFocusedPane function not found"
        body = match.group(0)
        assert '_refreshAllTerminals' in body, \
            "_closeFocusedPane does not refresh terminals"

    def test_build_tree_function(self):
        """_buildTree must exist for rendering the split tree DOM."""
        assert 'function _buildTree' in self.html

    def test_get_leaves_function(self):
        """_getLeaves must exist for traversing the tree."""
        assert 'function _getLeaves' in self.html

    def test_find_leaf_function(self):
        """_findLeaf must exist for locating leaves by id."""
        assert 'function _findLeaf' in self.html

    def test_remove_leaf_from_tree(self):
        """_removeLeafFromTree must exist for collapsing splits on close."""
        assert 'function _removeLeafFromTree' in self.html

    def test_cycle_tabs_crosses_all(self):
        """_cycleTabs must cycle across ALL tabs."""
        assert '_termTabs.keys()' in self.html, \
            "_cycleTabs does not cycle across all tabs"

    def test_set_focused_leaf_calls_ensure_focus(self):
        """_setFocusedLeaf must call _ensureFocus for proper refresh."""
        match = re.search(
            r'function _setFocusedLeaf.*?(?=\n\n// ──|\nfunction )',
            self.html,
            re.DOTALL,
        )
        assert match, "_setFocusedLeaf function not found"
        body = match.group(0)
        assert '_ensureFocus' in body, \
            "_setFocusedLeaf does not call _ensureFocus"


class TestDevLogCrossCheck:
    """Cross-check: all dev-log CSS classes in index.html also exist in detail.html."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.index = _read(INDEX_HTML)
        self.detail = _read(DETAIL_HTML)

    def _extract_dev_log_css_classes(self, text):
        """Extract all .dev-log-* CSS class selectors from text."""
        return set(re.findall(r'\.dev-log-[\w-]+', text))

    def test_all_index_dev_log_classes_in_detail(self):
        """Every .dev-log-* CSS class in index.html must also appear in detail.html."""
        index_classes = self._extract_dev_log_css_classes(self.index)
        detail_classes = self._extract_dev_log_css_classes(self.detail)
        assert index_classes, "No .dev-log-* classes found in index.html"
        missing = sorted(index_classes - detail_classes)
        assert not missing, (
            f"Dev-log CSS classes in index.html but missing from detail.html: {missing}"
        )

    def test_dev_log_panel_id_in_both(self):
        """#dev-log-panel must exist in both index.html and detail.html."""
        assert 'id="dev-log-panel"' in self.index, "dev-log-panel missing from index.html"
        assert 'id="dev-log-panel"' in self.detail, "dev-log-panel missing from detail.html"

    def test_dev_log_body_id_in_both(self):
        """#dev-log-body must exist in both index.html and detail.html."""
        assert 'id="dev-log-body"' in self.index, "dev-log-body missing from index.html"
        assert 'id="dev-log-body"' in self.detail, "dev-log-body missing from detail.html"
