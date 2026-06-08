"""
TDD tests for bug fixes described in prds/bugs.md and subsequent reports.
These tests FAIL before fixes and PASS after.

All tests are static analysis: they read source files and assert
the expected code pattern is present.
"""

import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATES = os.path.join(REPO, "savant", "templates")
STATIC_JS = os.path.join(REPO, "savant", "static", "js")


def _read(path):
    with open(path) as f:
        return f.read()


# ---------------------------------------------------------------------------
# Bug 1: terminal-view.js delegates to BrowserView terminal
# ---------------------------------------------------------------------------

def test_bug1_switchview_delegates_to_browserview():
    """
    switchView('terminal') must delegate to the BrowserView terminal
    (via terminalAPI.showDrawer) instead of activating the in-page terminal.
    """
    src = _read(os.path.join(STATIC_JS, "terminal-view.js"))

    assert 'showDrawer' in src, (
        "terminal-view.js must call terminalAPI.showDrawer to delegate "
        "to the BrowserView terminal."
    )
    assert 'hideDrawer' in src, (
        "terminal-view.js must call terminalAPI.hideDrawer when switching "
        "back to UI view."
    )


# ---------------------------------------------------------------------------
# Bug 2: No left action bar in detail.html
# ---------------------------------------------------------------------------

def test_bug2_detail_has_left_tab_bar():
    """
    detail.html must contain <div id="left-tab-bar"> for the sidebar.
    """
    src = _read(os.path.join(TEMPLATES, "detail.html"))

    assert 'id="left-tab-bar"' in src, (
        'Bug 2 fix missing: detail.html must contain <div id="left-tab-bar"> '
        "to show the left action/navigation bar on the session detail page."
    )


# ---------------------------------------------------------------------------
# Bug 3: App goes into mobile mode
# ---------------------------------------------------------------------------

def test_bug3_index_has_min_width_on_body():
    """
    index.html must have min-width on html/body to prevent mobile layout.
    """
    src = _read(os.path.join(TEMPLATES, "index.html"))

    has_min_width = bool(
        re.search(r"(?:html|body)\s*\{[^}]*min-width\s*:\s*\d{3,}px", src, re.DOTALL)
    )
    assert has_min_width, (
        "Bug 3 fix missing (index.html): add `min-width: 900px` (or similar) to the "
        "html or body CSS rule to prevent Electron from triggering mobile media queries."
    )


def test_bug3_detail_has_min_width_on_body():
    """Same as above but for detail.html."""
    src = _read(os.path.join(TEMPLATES, "detail.html"))

    has_min_width = bool(
        re.search(r"(?:html|body)\s*\{[^}]*min-width\s*:\s*\d{3,}px", src, re.DOTALL)
    )
    assert has_min_width, (
        "Bug 3 fix missing (detail.html): add `min-width: 900px` (or similar) to the "
        "html or body CSS rule to prevent the session detail page from going into mobile mode."
    )


# ---------------------------------------------------------------------------
# Bug 5: X and ? buttons visible when terminal is hidden
# ---------------------------------------------------------------------------

def test_bug5_switchview_controls_close_and_tips_buttons():
    """
    switchView() must reference both 'left-tab-close' and 'left-tab-tips'
    to show/hide those buttons based on terminal view state.
    """
    src = _read(os.path.join(STATIC_JS, "terminal-view.js"))

    fn_match = re.search(
        r"function switchView\b(.+?)(?=\nfunction |\Z)", src, re.DOTALL
    )
    assert fn_match, "switchView function not found in terminal-view.js"

    fn_body = fn_match.group(1)

    has_close = "left-tab-close" in fn_body
    has_tips = "left-tab-tips" in fn_body

    assert has_close and has_tips, (
        "Bug 5 fix missing: switchView() must reference both 'left-tab-close' and "
        "'left-tab-tips' to show/hide those buttons based on whether the terminal "
        "view is active. They should be hidden when view === 'ui'."
    )


# ---------------------------------------------------------------------------
# Bug 6: Logs panel should sit next to the left sidebar (not cover it)
# ---------------------------------------------------------------------------

def test_bug6_dev_log_panel_preserves_sidebar():
    """
    The .dev-log-panel must have left: 48px and z-index below sidebar (10000).
    """
    src = _read(os.path.join(TEMPLATES, "index.html"))

    panel_match = re.search(
        r"\.dev-log-panel\s*\{([^}]+)\}", src, re.DOTALL
    )
    assert panel_match, ".dev-log-panel CSS rule not found in index.html"

    panel_css = panel_match.group(1)

    has_48px = bool(re.search(r"left\s*:\s*48px", panel_css))
    assert has_48px, (
        "Bug 6 fix: .dev-log-panel must have `left: 48px` so the sidebar remains visible."
    )

    z_match = re.search(r"z-index\s*:\s*(\d+)", panel_css)
    assert z_match, "z-index not found in .dev-log-panel"
    z_val = int(z_match.group(1))
    assert z_val < 10000, (
        f"Bug 6 fix: .dev-log-panel z-index ({z_val}) must be below sidebar z-index (10000)."
    )
    assert z_val >= 9999, (
        f"Bug 6 fix: .dev-log-panel z-index ({z_val}) must be >= 9999."
    )


# ---------------------------------------------------------------------------
# Bug 7: Terminal X button closes ALL tabs instead of just the active tab
# ---------------------------------------------------------------------------

def test_bug7_terminal_close_button_uses_closetab():
    """
    The Back to Session button in detail.html must call switchView('ui').
    """
    src = _read(os.path.join(TEMPLATES, "detail.html"))

    close_btn_match = re.search(
        r'<button[^>]+title=["\']Back to Session["\'][^>]*>',
        src,
    )
    assert close_btn_match, (
        'No button with title="Back to Session" found in detail.html'
    )

    btn_html = close_btn_match.group(0)
    calls_switch_view = bool(re.search(r"""switchView\s*\(\s*['"]ui['"]\s*\)""", btn_html))
    assert calls_switch_view, (
        "The 'Back to Session' button must call `switchView('ui')`."
    )


# ---------------------------------------------------------------------------
# Bug 10: In-page terminal removed — only BrowserView terminal exists
# ---------------------------------------------------------------------------

def test_bug10_no_inpage_terminal_code():
    """
    terminal-view.js must be a thin stub that delegates to BrowserView.
    It must NOT contain in-page terminal functions like _tvAddTab, _tvSplitPane.
    """
    src = _read(os.path.join(STATIC_JS, "terminal-view.js"))

    assert '_tvAddTab' not in src, "Dead code: _tvAddTab still in terminal-view.js"
    assert '_tvSplitPane' not in src, "Dead code: _tvSplitPane still in terminal-view.js"
    assert '_tvReconnect' not in src, "Dead code: _tvReconnect still in terminal-view.js"
    assert '_tvBuildPaneDOM' not in src, "Dead code: _tvBuildPaneDOM still in terminal-view.js"


def test_bug10_index_no_inpage_terminal_html():
    """
    index.html must not have the in-page terminal HTML (tab-strip, terminal-body).
    """
    src = _read(os.path.join(TEMPLATES, "index.html"))

    assert 'id="terminal-tab-strip"' not in src, \
        "Dead code: #terminal-tab-strip still in index.html"
    assert 'id="tv-tabs"' not in src, \
        "Dead code: #tv-tabs still in index.html"
