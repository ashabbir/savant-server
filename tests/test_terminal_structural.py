"""
Structural regression tests for terminal.html and main.js terminal management.

These tests read the source files and verify critical code patterns, function
signatures, data flow, and known-bug-fix invariants. They catch regressions
where a refactor accidentally removes or breaks terminal functionality.

Every test here corresponds to a real bug that shipped and broke the terminal.
"""

import os
import re

import pytest

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
TERMINAL_HTML = os.path.join(_PROJECT_ROOT, 'terminal.html')
MAIN_JS = os.path.join(_PROJECT_ROOT, 'main.js')
PRELOAD_JS = os.path.join(_PROJECT_ROOT, 'preload.js')


def _read(filepath):
    with open(filepath) as f:
        return f.read()


def _extract_function(src, name):
    """Extract a JS function body by name (greedy up to next top-level function or section comment)."""
    pattern = rf'(?:async\s+)?function\s+{re.escape(name)}\b.*?(?=\n(?:async\s+)?function\s|\n// ── Section|\Z)'
    m = re.search(pattern, src, re.DOTALL)
    return m.group(0) if m else None


# ═══════════════════════════════════════════════════════════════════════════════
# TERMINAL.HTML — Tree data model
# ═══════════════════════════════════════════════════════════════════════════════

class TestTreeDataModel:
    """The tree-based split model must have all required primitives."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.src = _read(TERMINAL_HTML)

    def test_tree_root_variable(self):
        assert '_treeRoot' in self.src

    def test_focused_leaf_variable(self):
        assert '_focusedLeafId' in self.src

    def test_get_leaves_function(self):
        assert 'function _getLeaves' in self.src

    def test_find_leaf_function(self):
        assert 'function _findLeaf' in self.src

    def test_find_parent_function(self):
        assert 'function _findParent' in self.src

    def test_find_leaf_by_tab_id(self):
        assert 'function _findLeafByTabId' in self.src

    def test_remove_leaf_from_tree(self):
        assert 'function _removeLeafFromTree' in self.src

    def test_new_node_id(self):
        assert 'function _newNodeId' in self.src


# ═══════════════════════════════════════════════════════════════════════════════
# BUG FIX: _findParent must default to _treeRoot
# Without this, splits and closes at non-root positions silently fail.
# ═══════════════════════════════════════════════════════════════════════════════

class TestFindParentDefaultsToRoot:
    """_findParent was called without the root arg — always returned null."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.src = _read(TERMINAL_HTML)

    def test_find_parent_has_default(self):
        fn = _extract_function(self.src, '_findParent')
        assert fn, "_findParent not found"
        # Must set node = _treeRoot when node is undefined
        assert '_treeRoot' in fn, \
            "_findParent must default the node parameter to _treeRoot"

    def test_find_parent_not_called_without_root(self):
        """All call sites outside _findParent itself must either pass _treeRoot
        or rely on the default. Verify the default exists so calls with 1 arg work."""
        fn = _extract_function(self.src, '_findParent')
        assert fn
        assert re.search(r'node\s*===\s*undefined|node\s*=\s*_treeRoot', fn), \
            "_findParent must handle being called with only nodeId (no node arg)"


# ═══════════════════════════════════════════════════════════════════════════════
# BUG FIX: _buildTree must use detached pane Map, not getElementById
# After detaching panes from DOM, getElementById can't find them.
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildTreeDetachedPanes:
    """_buildTree detaches panes then _buildNodeDOM must retrieve them from a Map,
    NOT from document.getElementById (which can't find detached elements)."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.src = _read(TERMINAL_HTML)

    def test_detached_panes_map_exists(self):
        assert '_detachedPanes' in self.src, \
            "_detachedPanes Map must exist for storing detached pane elements"

    def test_build_tree_populates_detached_map(self):
        fn = _extract_function(self.src, '_buildTree')
        assert fn, "_buildTree not found"
        assert '_detachedPanes.set' in fn, \
            "_buildTree must store detached panes in _detachedPanes Map"

    def test_build_node_dom_reads_from_detached_map(self):
        fn = _extract_function(self.src, '_buildNodeDOM')
        assert fn, "_buildNodeDOM not found"
        assert '_detachedPanes.get' in fn, \
            "_buildNodeDOM must retrieve panes from _detachedPanes Map, not getElementById"

    def test_build_node_dom_does_not_use_getelementbyid_for_panes(self):
        fn = _extract_function(self.src, '_buildNodeDOM')
        assert fn
        # Should NOT have getElementById('term-pane-...') for pane retrieval
        assert not re.search(r"getElementById\(['\"]term-pane-", fn), \
            "_buildNodeDOM must NOT use getElementById for pane lookup (detached elements are invisible)"

    def test_build_tree_clears_map_after_rebuild(self):
        fn = _extract_function(self.src, '_buildTree')
        assert fn
        assert '_detachedPanes.clear()' in fn, \
            "_buildTree must clear _detachedPanes after rebuild to avoid memory leaks"


# ═══════════════════════════════════════════════════════════════════════════════
# BUG FIX: _buildTree must refresh all terminals after DOM rebuild
# After re-attaching xterm containers, canvas must be refreshed.
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuildTreeRefreshes:
    """After rebuilding the DOM tree, terminals must be refreshed and focused."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.src = _read(TERMINAL_HTML)

    def test_build_tree_calls_refresh(self):
        fn = _extract_function(self.src, '_buildTree')
        assert fn
        assert '_refreshAllTerminals' in fn, \
            "_buildTree must call _refreshAllTerminals after DOM rebuild"

    def test_build_tree_calls_ensure_focus(self):
        fn = _extract_function(self.src, '_buildTree')
        assert fn
        assert '_ensureFocus' in fn, \
            "_buildTree must call _ensureFocus after DOM rebuild"


# ═══════════════════════════════════════════════════════════════════════════════
# BUG FIX: termAddTab must add leaf to tree (not leave it orphaned)
# When _treeRoot already exists and no leafId is passed, the new leaf
# must be added to the tree — otherwise xterm is wired but never shown.
# ═══════════════════════════════════════════════════════════════════════════════

class TestTermAddTabAddsLeaf:
    """termAddTab must always add the new leaf to the tree."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.src = _read(TERMINAL_HTML)

    def test_handles_first_tab(self):
        fn = _extract_function(self.src, 'termAddTab')
        assert fn
        assert '_treeRoot = newLeaf' in fn or '_treeRoot = newLeaf;' in fn, \
            "termAddTab must set _treeRoot for the first tab"

    def test_handles_additional_tab_without_leaf_id(self):
        fn = _extract_function(self.src, 'termAddTab')
        assert fn
        # When _treeRoot exists and no leafId, must create a split to add the new leaf
        assert 'else if (!leafId)' in fn, \
            "termAddTab must handle the case where _treeRoot exists but no leafId is given"

    def test_calls_build_tree(self):
        fn = _extract_function(self.src, 'termAddTab')
        assert fn
        assert '_buildTree()' in fn, \
            "termAddTab must call _buildTree to render the new leaf"


# ═══════════════════════════════════════════════════════════════════════════════
# BUG FIX: xterm module loading must have error handling and retry
# If __FLASK_BASE__ isn't set yet, import() fails silently.
# ═══════════════════════════════════════════════════════════════════════════════

class TestXtermLoadRetry:
    """termAddTab must catch import errors and retry."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.src = _read(TERMINAL_HTML)

    def test_has_try_catch(self):
        fn = _extract_function(self.src, 'termAddTab')
        assert fn
        assert 'try {' in fn and 'catch' in fn, \
            "termAddTab must wrap xterm module loading in try/catch"

    def test_has_retry(self):
        fn = _extract_function(self.src, 'termAddTab')
        assert fn
        # Should have a second try/catch for retry
        assert fn.count('catch') >= 2, \
            "termAddTab must retry xterm module loading on first failure"


# ═══════════════════════════════════════════════════════════════════════════════
# BUG FIX: _ensureFocus must focus FIRST, then fit, then focus again
# Cursor blink doesn't restart if focus() comes after fit()+refresh().
# ═══════════════════════════════════════════════════════════════════════════════

class TestEnsureFocusOrder:
    """_ensureFocus must call xterm.focus() before AND after fit/refresh."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.src = _read(TERMINAL_HTML)

    def test_focus_before_fit(self):
        fn = _extract_function(self.src, '_ensureFocus')
        assert fn
        # Find positions of first focus() and first fit()
        focus_pos = fn.find('xterm.focus()')
        fit_pos = fn.find('fitAddon.fit()')
        assert focus_pos > 0 and fit_pos > 0, \
            "_ensureFocus must call both xterm.focus() and fitAddon.fit()"
        assert focus_pos < fit_pos, \
            "_ensureFocus must call xterm.focus() BEFORE fitAddon.fit()"

    def test_double_focus(self):
        fn = _extract_function(self.src, '_ensureFocus')
        assert fn
        assert fn.count('xterm.focus()') >= 2, \
            "_ensureFocus must call xterm.focus() twice (before and after fit/refresh)"

    def test_no_xterm_tab_id_reference(self):
        """Old bug: _ensureFocus referenced tab.xterm._tabId which doesn't exist."""
        fn = _extract_function(self.src, '_ensureFocus')
        assert fn
        assert '_tabId' not in fn, \
            "_ensureFocus must NOT reference xterm._tabId (doesn't exist)"


# ═══════════════════════════════════════════════════════════════════════════════
# Split pane management
# ═══════════════════════════════════════════════════════════════════════════════

class TestSplitPaneManagement:
    """Split operations must exist and refresh terminals."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.src = _read(TERMINAL_HTML)

    def test_split_focused_pane_exists(self):
        assert 'function _splitFocusedPane' in self.src

    def test_close_focused_pane_exists(self):
        assert 'function _closeFocusedPane' in self.src

    def test_split_refreshes(self):
        fn = _extract_function(self.src, '_splitFocusedPane')
        assert fn
        assert '_refreshAllTerminals' in fn

    def test_close_refreshes(self):
        fn = _extract_function(self.src, '_closeFocusedPane')
        assert fn
        assert '_refreshAllTerminals' in fn

    def test_split_creates_new_terminal(self):
        fn = _extract_function(self.src, '_splitFocusedPane')
        assert fn
        assert '_createTerminalForLeaf' in fn, \
            "_splitFocusedPane must create a new terminal for the second pane"

    def test_close_disposes_xterm(self):
        fn = _extract_function(self.src, '_closeFocusedPane')
        assert fn
        assert 'xterm.dispose()' in fn, \
            "_closeFocusedPane must dispose the xterm instance"

    def test_close_removes_leaf(self):
        fn = _extract_function(self.src, '_closeFocusedPane')
        assert fn
        assert '_removeLeafFromTree' in fn, \
            "_closeFocusedPane must remove the leaf from the tree"


# ═══════════════════════════════════════════════════════════════════════════════
# Tab cycling
# ═══════════════════════════════════════════════════════════════════════════════

class TestTabCycling:
    """Tab cycling must work across ALL tabs, not just one pane."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.src = _read(TERMINAL_HTML)

    def test_cycle_tabs_uses_all_tabs(self):
        fn = _extract_function(self.src, '_cycleTabs')
        assert fn
        assert '_termTabs.keys()' in fn, \
            "_cycleTabs must iterate all tabs, not filter by slot"


# ═══════════════════════════════════════════════════════════════════════════════
# Focus recovery — visibility and window focus
# ═══════════════════════════════════════════════════════════════════════════════

class TestFocusRecovery:
    """Terminal must recover focus/rendering on visibility changes."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.src = _read(TERMINAL_HTML)

    def test_visibilitychange_listener(self):
        assert 'visibilitychange' in self.src

    def test_window_focus_listener(self):
        assert re.search(r"window\.addEventListener\(['\"]focus['\"]", self.src)

    def test_focus_and_refresh_command(self):
        assert "'focus-and-refresh'" in self.src or '"focus-and-refresh"' in self.src

    def test_document_mousedown_reclaims_focus(self):
        """Any click in the terminal must reclaim xterm focus (Electron eats first click)."""
        assert re.search(
            r"document\.addEventListener\(['\"]mousedown['\"]", self.src
        ), "document-level mousedown handler must exist for focus recovery"

    def test_refresh_all_in_focus_recovery(self):
        assert '_refreshAllTerminals' in self.src


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN.JS — BrowserView management
# ═══════════════════════════════════════════════════════════════════════════════

class TestMainJSBrowserView:
    """BrowserView lifecycle and bounds must be correct."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.js = _read(MAIN_JS)

    def test_show_adds_browser_view(self):
        fn = _extract_function(self.js, 'showTermView')
        assert fn
        assert 'addBrowserView' in fn

    def test_hide_removes_browser_view(self):
        fn = _extract_function(self.js, 'hideTermView')
        assert fn
        assert 'removeBrowserView' in fn

    def test_bounds_respect_sidebar(self):
        assert '_SIDEBAR_WIDTH' in self.js
        fn = _extract_function(self.js, '_calcTermBounds')
        assert fn
        assert '_SIDEBAR_WIDTH' in fn, \
            "_calcTermBounds must account for the left sidebar"

    def test_bounds_respect_topbar(self):
        assert '_TOPBAR_HEIGHT' in self.js
        fn = _extract_function(self.js, '_calcTermBounds')
        assert fn
        assert '_TOPBAR_HEIGHT' in fn

    def test_bounds_respect_statusbar(self):
        assert '_STATUSBAR_HEIGHT' in self.js
        fn = _extract_function(self.js, '_calcTermBounds')
        assert fn
        assert '_STATUSBAR_HEIGHT' in fn

    def test_bounds_respect_rightbar(self):
        assert '_RIGHTBAR_WIDTH' in self.js
        fn = _extract_function(self.js, '_calcTermBounds')
        assert fn
        assert '_RIGHTBAR_WIDTH' in fn


# ═══════════════════════════════════════════════════════════════════════════════
# BUG FIX: Flask base URL must be injected BEFORE sending commands
# Without await, add-tab fires before __FLASK_BASE__ is set.
# ═══════════════════════════════════════════════════════════════════════════════

class TestFlaskBaseInjection:
    """_injectFlaskBase must be awaited before terminal commands."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.js = _read(MAIN_JS)

    def test_inject_is_async(self):
        fn = _extract_function(self.js, '_injectFlaskBase')
        assert fn
        assert fn.strip().startswith('async function'), \
            "_injectFlaskBase must be async"

    def test_show_awaits_inject(self):
        fn = _extract_function(self.js, 'showTermView')
        assert fn
        assert 'await _injectFlaskBase()' in fn, \
            "showTermView must await _injectFlaskBase before sending commands"

    def test_width_always_100(self):
        fn = _extract_function(self.js, '_activeWidthPct')
        assert fn
        assert 'return 100' in fn, \
            "Terminal must always fill 100% of content area"


# ═══════════════════════════════════════════════════════════════════════════════
# BrowserView focus event
# ═══════════════════════════════════════════════════════════════════════════════

class TestBrowserViewFocusEvent:
    """When BrowserView gets focus, it must send focus-and-refresh."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.js = _read(MAIN_JS)

    def test_webcontents_focus_handler(self):
        assert re.search(
            r'termView\.webContents\.on\(["\']focus["\']',
            self.js,
        ), "termView must have a webContents 'focus' event handler"

    def test_sends_focus_and_refresh(self):
        fn = _extract_function(self.js, 'createTermView')
        assert fn
        assert 'focus-and-refresh' in fn


# ═══════════════════════════════════════════════════════════════════════════════
# Resize handle removed (terminal fills 100%)
# ═══════════════════════════════════════════════════════════════════════════════

class TestNoResizeHandle:
    """Resize handle must be hidden — terminal always fills content area."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.src = _read(TERMINAL_HTML)

    def test_resize_handle_hidden(self):
        assert re.search(r'id="resize-handle"[^>]*display:\s*none', self.src), \
            "Resize handle must be hidden (display:none)"

    def test_no_left_padding(self):
        """App container should not have padding-left for the resize handle."""
        assert 'padding-left: 0' in self.src or 'padding-left:0' in self.src


# ═══════════════════════════════════════════════════════════════════════════════
# IPC channel consistency
# ═══════════════════════════════════════════════════════════════════════════════

class TestIPCChannels:
    """IPC channels must match between main.js and preload.js."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.main = _read(MAIN_JS)
        self.preload = _read(PRELOAD_JS)

    def test_add_tab_command(self):
        assert 'terminal:command' in self.main
        assert 'terminal:command' in self.preload

    def test_drawer_state(self):
        assert 'terminal:drawer-state' in self.main
        assert 'terminal:drawer-state' in self.preload

    def test_toggle_drawer(self):
        assert 'terminal:toggle-drawer' in self.main
        assert 'terminal:toggle-drawer' in self.preload

    def test_show_drawer(self):
        assert 'terminal:show-drawer' in self.main
        assert 'terminal:show-drawer' in self.preload

    def test_hide_drawer(self):
        assert 'terminal:hide-drawer' in self.main
        assert 'terminal:hide-drawer' in self.preload
