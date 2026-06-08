"""TDD tests for shared frontend components (Phase 2).

Tests cover:
  - /static/js/utils.js — shared utility functions
  - /static/js/status-bar.js — shared status bar module
  - /static/css/shared.css — shared stylesheet
  - index.html + detail.html integration — both import shared modules
"""
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATIC_DIR = os.path.join(REPO, "savant", "static")
TEMPLATES_DIR = os.path.join(REPO, "savant", "templates")
INDEX_HTML = os.path.join(TEMPLATES_DIR, "index.html")
DETAIL_HTML = os.path.join(TEMPLATES_DIR, "detail.html")


def _read(path):
    with open(path) as f:
        return f.read()


# ─── /static/js/utils.js ─────────────────────────────────────────────────────

class TestUtilsModule:
    """Shared utility functions extracted into /static/js/utils.js."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.path = os.path.join(STATIC_DIR, "js", "utils.js")
        assert os.path.exists(self.path), "utils.js must exist"
        self.src = _read(self.path)

    def test_exports_escapeHtml(self):
        assert "function escapeHtml" in self.src

    def test_exports_formatTs(self):
        assert "function formatTs" in self.src

    def test_exports_formatDate(self):
        assert "function formatDate" in self.src

    def test_exports_formatDateTime(self):
        assert "function formatDateTime" in self.src

    def test_exports_formatSize(self):
        assert "function formatSize" in self.src

    def test_exports_formatDuration(self):
        assert "function formatDuration" in self.src

    def test_exports_showLoadingThenNavigate(self):
        assert "function showLoadingThenNavigate" in self.src

    def test_exports_showToast(self):
        assert "function showToast" in self.src

    def test_escapeHtml_handles_ampersand(self):
        assert "&amp;" in self.src

    def test_escapeHtml_handles_angle_brackets(self):
        assert "&lt;" in self.src and "&gt;" in self.src

    def test_formatDuration_has_hours(self):
        """formatDuration should handle hours/minutes/seconds."""
        assert "h " in self.src and "m " in self.src

    def test_valid_js_syntax(self):
        """File should not have obvious syntax errors."""
        # No unclosed braces — count opening/closing
        opens = self.src.count("{")
        closes = self.src.count("}")
        assert opens == closes, f"Brace mismatch: {opens} open, {closes} close"


# ─── /static/js/status-bar.js ────────────────────────────────────────────────

class TestStatusBarModule:
    """Shared status bar module extracted into /static/js/status-bar.js."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.path = os.path.join(STATIC_DIR, "js", "status-bar.js")
        assert os.path.exists(self.path), "status-bar.js must exist"
        self.src = _read(self.path)

    def test_exports_initStatusBar(self):
        assert "function initStatusBar" in self.src

    def test_exports_updateStatusBarClock(self):
        assert "function updateStatusBarClock" in self.src

    def test_exports_tickStatusBarRefresh(self):
        assert "function tickStatusBarRefresh" in self.src

    def test_exports_updateStatusBarMcp(self):
        assert "function updateStatusBarMcp" in self.src

    def test_accepts_page_specific_hooks(self):
        """initStatusBar should accept overrides for page-specific functions."""
        assert "updateSessions" in self.src or "sessionsHook" in self.src or "options" in self.src

    def test_has_clock_interval(self):
        assert "setInterval" in self.src

    def test_has_mcp_health_fetch(self):
        assert "/api/mcp/health" in self.src

    def test_valid_js_syntax(self):
        opens = self.src.count("{")
        closes = self.src.count("}")
        assert opens == closes


# ─── /static/css/shared.css ──────────────────────────────────────────────────

class TestSharedCSS:
    """Shared CSS extracted into /static/css/shared.css."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.path = os.path.join(STATIC_DIR, "css", "shared.css")
        assert os.path.exists(self.path), "shared.css must exist"
        self.src = _read(self.path)

    def test_has_css_variables(self):
        """Should define theme variables in :root."""
        assert ":root" in self.src
        assert "--bg" in self.src
        assert "--cyan" in self.src

    def test_has_header_styles(self):
        assert ".header" in self.src

    def test_has_mode_switcher(self):
        assert ".mode-switcher" in self.src
        assert ".mode-btn" in self.src

    def test_has_left_tab_bar(self):
        assert ".left-tab-btn" in self.src
        assert ".left-tab-action" in self.src

    def test_uses_css_variables_not_hardcoded(self):
        """Left tab bar should use CSS variables, not hardcoded RGBA."""
        # Find left-tab-btn section
        match = re.search(r'\.left-tab-btn\s*\{[^}]+\}', self.src)
        if match:
            block = match.group()
            assert "var(--" in block, "left-tab-btn should use CSS variables"

    def test_has_status_bar(self):
        assert "#bottom-status-bar" in self.src or ".status-bar-segment" in self.src

    def test_has_toast_styles(self):
        assert ".toast-container" in self.src

    def test_has_modal_styles(self):
        assert ".modal-overlay" in self.src
        assert ".modal" in self.src


# ─── Integration: both pages import shared modules ───────────────────────────

class TestSharedModuleIntegration:
    """Both index.html and detail.html should import shared modules."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.index = _read(INDEX_HTML)
        self.detail = _read(DETAIL_HTML)

    # CSS imports
    def test_index_imports_shared_css(self):
        assert "shared.css" in self.index

    def test_detail_imports_shared_css(self):
        assert "shared.css" in self.detail

    # JS imports
    def test_index_imports_utils_js(self):
        assert "utils.js" in self.index

    def test_detail_imports_utils_js(self):
        assert "utils.js" in self.detail

    def test_index_imports_status_bar_js(self):
        assert "status-bar.js" in self.index

    def test_detail_imports_status_bar_js(self):
        assert "status-bar.js" in self.detail

    # No duplicate inline definitions
    def test_detail_no_inline_escapeHtml(self):
        """detail.html should NOT define escapeHtml inline (it's in utils.js)."""
        # Allow the <script src="utils.js"> import, but not an inline function definition
        lines = self.detail.split("\n")
        inline_defs = [
            i for i, l in enumerate(lines, 1)
            if "function escapeHtml" in l and "<script" not in l
        ]
        assert len(inline_defs) == 0, f"Found inline escapeHtml at lines: {inline_defs}"

    def test_detail_no_inline_formatTs(self):
        lines = self.detail.split("\n")
        inline_defs = [
            i for i, l in enumerate(lines, 1)
            if "function formatTs" in l and "<script" not in l
        ]
        assert len(inline_defs) == 0, f"Found inline formatTs at lines: {inline_defs}"

    def test_detail_no_inline_showToast(self):
        lines = self.detail.split("\n")
        inline_defs = [
            i for i, l in enumerate(lines, 1)
            if "function showToast" in l and "<script" not in l
        ]
        assert len(inline_defs) == 0, f"Found inline showToast at lines: {inline_defs}"

    def test_index_no_inline_statusbar(self):
        """index.html should NOT define updateStatusBarClock inline."""
        lines = self.index.split("\n")
        inline_defs = [
            i for i, l in enumerate(lines, 1)
            if "function updateStatusBarClock" in l and "<script" not in l
        ]
        assert len(inline_defs) == 0, f"Found inline status bar at lines: {inline_defs}"

    def test_detail_no_inline_statusbar(self):
        lines = self.detail.split("\n")
        inline_defs = [
            i for i, l in enumerate(lines, 1)
            if "function updateStatusBarClock" in l and "<script" not in l
        ]
        assert len(inline_defs) == 0, f"Found inline status bar at lines: {inline_defs}"

    def test_detail_uses_css_variables_for_left_tabs(self):
        """detail.html's left tab styles should use CSS vars, not hardcoded RGBA."""
        # After refactor, left-tab-btn styles should be in shared.css only
        # Detail should not redefine left-tab-btn with hardcoded colors
        style_match = re.search(
            r'<style[^>]*>(.*?)</style>', self.detail, re.DOTALL
        )
        if style_match:
            detail_css = style_match.group(1)
            # If left-tab-btn is in inline CSS, it should use var()
            btn_match = re.search(r'\.left-tab-btn\s*\{[^}]+\}', detail_css)
            if btn_match:
                assert "rgba(" not in btn_match.group(), \
                    "left-tab-btn should use CSS variables, not hardcoded RGBA"


class TestSharedHeaderComponent:
    """Both pages must use the same _header.html Jinja component."""

    header_path = os.path.join(REPO, "savant", "templates", "components", "_header.html")

    def test_header_component_exists(self):
        assert os.path.isfile(self.header_path)

    def test_header_exports_macro(self):
        src = open(self.header_path).read()
        assert "macro savant_header" in src

    def test_header_has_logo_svg(self):
        src = open(self.header_path).read()
        assert "savant-logo" in src
        assert 'viewBox="0 0 64 64"' in src

    def test_index_imports_header(self):
        src = open(os.path.join(REPO, "savant", "templates", "index.html")).read()
        assert 'from "components/_header.html" import savant_header' in src

    def test_detail_imports_header(self):
        src = open(os.path.join(REPO, "savant", "templates", "detail.html")).read()
        assert 'from "components/_header.html" import savant_header' in src

    def test_index_calls_header_with_index_page(self):
        src = open(os.path.join(REPO, "savant", "templates", "index.html")).read()
        assert 'savant_header(page="index")' in src

    def test_detail_calls_header_with_detail_page(self):
        src = open(os.path.join(REPO, "savant", "templates", "detail.html")).read()
        assert 'savant_header(page="detail")' in src

    def test_index_no_inline_logo_svg(self):
        """index.html should NOT have an inline SVG logo — it comes from the macro."""
        src = open(os.path.join(REPO, "savant", "templates", "index.html")).read()
        # The SVG should only appear via the macro, not as raw inline HTML
        assert src.count('class="savant-logo"') == 0, \
            "index.html should not have inline savant-logo SVG — it should come from _header.html macro"

    def test_detail_no_inline_logo_svg(self):
        """detail.html should NOT have an inline SVG logo — it comes from the macro."""
        src = open(os.path.join(REPO, "savant", "templates", "detail.html")).read()
        assert src.count('class="savant-logo"') == 0, \
            "detail.html should not have inline savant-logo SVG — it should come from _header.html macro"
