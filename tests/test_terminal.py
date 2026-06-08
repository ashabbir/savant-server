"""Tests for terminal preferences storage via Flask API."""
import json
import os
import re
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TERMINAL_HTML = os.path.join(REPO, "terminal.html")
MAIN_JS = os.path.join(REPO, "main.js")
STATIC_DIR = os.path.join(REPO, "savant", "static")


def _read(path):
    with open(path) as f:
        return f.read()


# ---------------------------------------------------------------------------
# Flask API tests
# ---------------------------------------------------------------------------

class TestTerminalPreferences:
    """Terminal preferences are stored in the preferences API."""

    def test_save_terminal_prefs(self, client):
        """Terminal prefs saved via /api/preferences."""
        payload = {
            "name": "TestUser",
            "work_week": [1, 2, 3, 4, 5],
            "enabled_providers": ["copilot"],
            "theme": "corporate",
            "terminal": {
                "externalTerminal": "iterm",
                "shell": "/bin/zsh",
                "fontSize": 14,
                "scrollback": 10000,
                "customCommand": "",
            },
        }
        res = client.post(
            "/api/preferences",
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert res.status_code == 200
        data = res.get_json()
        assert data["terminal"]["externalTerminal"] == "iterm"
        assert data["terminal"]["shell"] == "/bin/zsh"
        assert data["terminal"]["fontSize"] == 14
        assert data["terminal"]["scrollback"] == 10000

    def test_terminal_prefs_persist(self, client):
        """Terminal prefs persist across reads."""
        payload = {
            "name": "TestUser",
            "work_week": [1, 2, 3, 4, 5],
            "enabled_providers": ["copilot"],
            "theme": "corporate",
            "terminal": {
                "externalTerminal": "warp",
                "shell": "/bin/bash",
                "fontSize": 16,
                "scrollback": 3000,
                "customCommand": "myterm --dir {cwd}",
            },
        }
        client.post(
            "/api/preferences",
            data=json.dumps(payload),
            content_type="application/json",
        )
        res = client.get("/api/preferences")
        assert res.status_code == 200
        data = res.get_json()
        assert data["terminal"]["externalTerminal"] == "warp"
        assert data["terminal"]["customCommand"] == "myterm --dir {cwd}"

    def test_terminal_prefs_defaults(self, client):
        """When no terminal prefs set, should return empty or defaults."""
        res = client.get("/api/preferences")
        assert res.status_code == 200
        data = res.get_json()
        terminal = data.get("terminal", {})
        assert isinstance(terminal, dict)

    def test_terminal_prefs_update_partial(self, client):
        """Updating terminal prefs doesn't lose other prefs."""
        payload1 = {
            "name": "Ali",
            "work_week": [1, 2, 3],
            "enabled_providers": ["copilot", "cline"],
            "theme": "starcraft",
            "terminal": {
                "externalTerminal": "auto",
                "shell": "auto",
                "fontSize": 13,
                "scrollback": 5000,
                "customCommand": "",
            },
        }
        client.post("/api/preferences", data=json.dumps(payload1), content_type="application/json")
        payload2 = {
            "name": "Ali",
            "work_week": [1, 2, 3],
            "enabled_providers": ["copilot", "cline"],
            "theme": "starcraft",
            "terminal": {
                "externalTerminal": "kitty",
                "shell": "/usr/local/bin/fish",
                "fontSize": 18,
                "scrollback": 50000,
                "customCommand": "",
            },
        }
        res = client.post("/api/preferences", data=json.dumps(payload2), content_type="application/json")
        data = res.get_json()
        assert data["terminal"]["externalTerminal"] == "kitty"
        assert data["terminal"]["fontSize"] == 18
        assert data["name"] == "Ali"
        assert data["theme"] == "starcraft"

    def test_all_external_terminal_options(self, client):
        """Each external terminal option is accepted."""
        for term in ["auto", "iterm", "terminal", "warp", "alacritty", "kitty", "custom"]:
            payload = {
                "name": "Test",
                "work_week": [1],
                "enabled_providers": ["copilot"],
                "theme": "corporate",
                "terminal": {
                    "externalTerminal": term,
                    "shell": "auto",
                    "fontSize": 13,
                    "scrollback": 5000,
                    "customCommand": "/bin/test --dir {cwd}" if term == "custom" else "",
                },
            }
            res = client.post(
                "/api/preferences",
                data=json.dumps(payload),
                content_type="application/json",
            )
            assert res.status_code == 200
            data = res.get_json()
            assert data["terminal"]["externalTerminal"] == term


# ---------------------------------------------------------------------------
# VS Code Parity — PTY spawn (main.js static analysis)
# ---------------------------------------------------------------------------

class TestVSCodeParityPtySpawn:
    """Verify the PTY spawn configuration matches VS Code's defaults."""

    def test_pty_spawns_with_login_and_interactive_flags(self):
        """-l -i ensures login shell (loads /etc/profile, ~/.zprofile) AND
        interactive shell (loads ~/.zshrc, aliases, functions)."""
        src = _read(MAIN_JS)
        assert 'pty.spawn(shell, ["-l", "-i"]' in src, (
            "PTY must use ['-l', '-i'] — '-i' loads .zshrc/aliases that '-l' alone skips"
        )

    def test_pty_does_not_use_login_only_flag(self):
        """Regression: old code used only ['-l'] which skipped interactive profile."""
        src = _read(MAIN_JS)
        # Must not have the old single-flag form
        assert 'pty.spawn(shell, ["-l"],' not in src, (
            "Old single-flag pty.spawn(shell, [\"-l\"]) found — must be [\"-l\", \"-i\"]"
        )

    def test_pty_sets_colorterm_truecolor(self):
        """COLORTERM=truecolor enables 24-bit colour in bat, delta, git-delta, ls."""
        src = _read(MAIN_JS)
        assert 'COLORTERM: "truecolor"' in src, "COLORTERM=truecolor not set in PTY env"

    def test_pty_sets_term_program(self):
        """TERM_PROGRAM lets shell configs (.zshrc, starship, p10k) detect the terminal."""
        src = _read(MAIN_JS)
        assert 'TERM_PROGRAM: "Savant"' in src, "TERM_PROGRAM not set in PTY env"

    def test_pty_sets_term_program_version(self):
        """TERM_PROGRAM_VERSION set from app version."""
        src = _read(MAIN_JS)
        assert "TERM_PROGRAM_VERSION" in src
        assert "app.getVersion()" in src

    def test_pty_sets_term_xterm_256color(self):
        """TERM=xterm-256color is required for colour support."""
        src = _read(MAIN_JS)
        assert 'TERM: "xterm-256color"' in src

    def test_pty_name_is_xterm_256color(self):
        """node-pty name field must match TERM."""
        src = _read(MAIN_JS)
        assert 'name: "xterm-256color"' in src


# ---------------------------------------------------------------------------
# VS Code Parity — xterm.js options (terminal.html static analysis)
# ---------------------------------------------------------------------------

class TestVSCodeParityXtermOptions:
    """Verify all VS Code xterm.js option equivalents are set."""

    def test_mac_option_is_meta(self):
        """macOptionIsMeta makes Option key act as Meta/Escape prefix for readline
        shortcuts (e.g. Option+B = word back, Option+F = word forward)."""
        src = _read(TERMINAL_HTML)
        assert "macOptionIsMeta: true" in src, (
            "macOptionIsMeta: true missing — Option key shortcuts broken without it"
        )

    def test_alt_click_moves_cursor(self):
        """altClickMovesCursor: alt+click positions the cursor like VS Code."""
        src = _read(TERMINAL_HTML)
        assert "altClickMovesCursor: true" in src

    def test_right_click_selects_word(self):
        """rightClickSelectsWord: right-click selects the word under cursor."""
        src = _read(TERMINAL_HTML)
        assert "rightClickSelectsWord: true" in src

    def test_word_separator_configured(self):
        """wordSeparator affects double-click word selection boundaries."""
        src = _read(TERMINAL_HTML)
        assert "wordSeparator:" in src
        match = re.search(r"wordSeparator:\s*['\"]([^'\"]+)['\"]", src)
        assert match, "wordSeparator value not parseable"
        sep = match.group(1)
        for char in ["(", ")", "{", "["]:
            assert char in sep, f"wordSeparator should contain '{char}'"

    def test_fast_scroll_modifier_alt(self):
        """fastScrollModifier: 'alt' — alt+scroll = fast scroll like VS Code."""
        src = _read(TERMINAL_HTML)
        assert "fastScrollModifier: 'alt'" in src

    def test_fast_scroll_sensitivity_set(self):
        src = _read(TERMINAL_HTML)
        assert "fastScrollSensitivity:" in src

    def test_draw_bold_in_bright_colors(self):
        """drawBoldTextInBrightColors: bold text renders with bright colour variants."""
        src = _read(TERMINAL_HTML)
        assert "drawBoldTextInBrightColors: true" in src

    def test_line_height_set(self):
        """lineHeight adds breathing room between lines (VS Code default ~1.2)."""
        src = _read(TERMINAL_HTML)
        assert "lineHeight:" in src
        match = re.search(r"lineHeight:\s*([\d.]+)", src)
        assert match, "lineHeight value not parseable"
        assert float(match.group(1)) >= 1.0

    def test_overview_ruler_width(self):
        """overviewRulerWidth enables the minimap-style scroll ruler."""
        src = _read(TERMINAL_HTML)
        assert "overviewRulerWidth:" in src

    def test_cursor_style_block(self):
        """cursorStyle: 'block' matches VS Code default."""
        src = _read(TERMINAL_HTML)
        assert "cursorStyle: 'block'" in src

    def test_cursor_blink_enabled(self):
        src = _read(TERMINAL_HTML)
        assert "cursorBlink: true" in src

    def test_scrollback_at_least_10000(self):
        """VS Code default is 1000 — we use at least 10000."""
        src = _read(TERMINAL_HTML)
        match = re.search(r"scrollback:\s*(\d+)", src)
        assert match, "scrollback not found"
        assert int(match.group(1)) >= 10000, f"scrollback should be >= 10000, got {match.group(1)}"

    def test_selection_inactive_background_in_theme(self):
        """Dims selection when terminal is unfocused — VS Code does this."""
        src = _read(TERMINAL_HTML)
        assert "selectionInactiveBackground:" in src

    def test_selection_foreground_in_theme(self):
        src = _read(TERMINAL_HTML)
        assert "selectionForeground:" in src

    def test_font_family_has_monospace_fallback(self):
        """fontFamily must have system monospace fallbacks.
        Note: fontFamily uses outer double-quotes wrapping inner single-quoted names:
        fontFamily: "'JetBrains Mono','Menlo','Cascadia Code','Consolas',monospace"
        """
        src = _read(TERMINAL_HTML)
        # Outer quotes are double, inner are single
        match = re.search(r'fontFamily:\s*"([^"]+)"', src)
        assert match, (
            "fontFamily not found — expected format: fontFamily: \"'JetBrains Mono','Menlo',...\""
        )
        ff = match.group(1)
        assert any(f in ff for f in ["Menlo", "Consolas", "monospace"]), (
            f"fontFamily should include system monospace fallback, got: {ff}"
        )

    def test_allow_proposed_api(self):
        """allowProposedApi: true is required for some VS Code parity features."""
        src = _read(TERMINAL_HTML)
        assert "allowProposedApi: true" in src

    def test_tab_stop_width(self):
        """tabStopWidth should be set (VS Code uses 8 by default)."""
        src = _read(TERMINAL_HTML)
        assert "tabStopWidth:" in src


# ---------------------------------------------------------------------------
# VS Code Parity — WebLinksAddon (clickable URLs)
# ---------------------------------------------------------------------------

class TestVSCodeParityWebLinks:
    """VS Code terminals make URLs clickable via WebLinksAddon."""

    def test_web_links_static_asset_exists(self):
        """xterm-addon-web-links.mjs must be served from Flask static."""
        path = os.path.join(STATIC_DIR, "xterm-addon-web-links.mjs")
        assert os.path.exists(path), (
            "xterm-addon-web-links.mjs missing from savant/static/ — copy from node_modules"
        )

    def test_load_web_links_addon_function_exists(self):
        src = _read(TERMINAL_HTML)
        assert "async function _loadWebLinksAddon()" in src

    def test_load_web_links_imports_from_flask_static(self):
        src = _read(TERMINAL_HTML)
        assert "_getFlaskBase() + '/static/xterm-addon-web-links.mjs'" in src

    def test_create_xterm_accepts_web_links_addon_param(self):
        src = _read(TERMINAL_HTML)
        match = re.search(r"function _createXterm\(([^)]+)\)", src)
        assert match, "_createXterm signature not found"
        assert "webLinksAddon" in match.group(1), (
            "_createXterm must accept webLinksAddon as 3rd parameter"
        )

    def test_create_xterm_loads_web_links_addon(self):
        fn_start = _read(TERMINAL_HTML).index("function _createXterm(")
        fn_body = _read(TERMINAL_HTML)[fn_start:fn_start + 3000]
        fn_end = fn_body.index("\nfunction ", 10)
        fn_body = fn_body[:fn_end]
        assert "xterm.loadAddon(webLinksAddon)" in fn_body

    def test_web_links_addon_instantiated_per_tab(self):
        """Each new tab and each reconnected tab needs its own addon instance."""
        src = _read(TERMINAL_HTML)
        count = src.count("new WebLinksAddon()")
        assert count >= 2, (
            f"Expected >= 2 'new WebLinksAddon()' instantiations (termAddTab + _reconnect), found {count}"
        )

    def test_term_add_tab_loads_web_links(self):
        src = _read(TERMINAL_HTML)
        fn_start = src.index("async function termAddTab(")
        fn_body = src[fn_start:fn_start + 1500]
        assert "WebLinksAddon" in fn_body, "termAddTab must use WebLinksAddon"
        assert "_loadWebLinksAddon" in fn_body, "termAddTab must call _loadWebLinksAddon"

    def test_reconnect_loads_web_links(self):
        src = _read(TERMINAL_HTML)
        fn_start = src.index("async function _reconnect(")
        fn_body = src[fn_start:fn_start + 2000]
        assert "WebLinksAddon" in fn_body, "_reconnect must use WebLinksAddon"
        assert "_loadWebLinksAddon" in fn_body, "_reconnect must call _loadWebLinksAddon"


# ---------------------------------------------------------------------------
# VS Code Parity — ⌘K clear scrollback
# ---------------------------------------------------------------------------

class TestVSCodeParityClearScrollback:
    """VS Code clears scrollback with ⌘K — we must match this."""

    def test_xterm_clear_called_on_cmd_k(self):
        src = _read(TERMINAL_HTML)
        assert "xterm.clear()" in src, "xterm.clear() missing — ⌘K won't clear scrollback"

    def test_cmd_k_handled_in_xterm_key_handler(self):
        """Must be in attachCustomKeyEventHandler so it works when xterm has focus."""
        src = _read(TERMINAL_HTML)
        match = re.search(
            r"attachCustomKeyEventHandler\(.*?return true;\s*\}",
            src,
            re.DOTALL,
        )
        assert match, "attachCustomKeyEventHandler handler not found"
        assert "xterm.clear()" in match.group(0), (
            "⌘K clear must be inside attachCustomKeyEventHandler"
        )

    def test_help_popup_documents_cmd_k(self):
        src = _read(TERMINAL_HTML)
        # Popup div has id="term-help-popup" on it, not just the class name alone
        popup_start = src.index('id="term-help-popup"')
        popup_html = src[popup_start:popup_start + 3000]
        assert "⌘ K" in popup_html or "⌘K" in popup_html, (
            "Help popup must document ⌘K shortcut"
        )
        assert "Clear" in popup_html, "Help popup must label ⌘K as 'Clear scrollback'"


# ---------------------------------------------------------------------------
# VS Code Parity — ⌘C smart copy
# ---------------------------------------------------------------------------

class TestVSCodeParitySmartCopy:
    """VS Code ⌘C copies selection if present, otherwise sends SIGINT."""

    def test_cmd_c_checks_has_selection(self):
        """If xterm has a selection, copy it; otherwise let ⌘C pass through as Ctrl+C."""
        src = _read(TERMINAL_HTML)
        handler = re.search(
            r"attachCustomKeyEventHandler\(.*?return true;\s*\}",
            src,
            re.DOTALL,
        )
        assert handler, "attachCustomKeyEventHandler not found"
        body = handler.group(0)
        assert "hasSelection()" in body, (
            "⌘C handler must call xterm.hasSelection() to decide copy vs SIGINT"
        )

    def test_cmd_c_copies_via_execcommand(self):
        src = _read(TERMINAL_HTML)
        handler = re.search(
            r"attachCustomKeyEventHandler\(.*?return true;\s*\}",
            src,
            re.DOTALL,
        )
        assert handler
        body = handler.group(0)
        # clipboard API or execCommand copy
        has_copy = "execCommand('copy')" in body or "clipboard.writeText" in body or "navigator.clipboard" in body
        assert has_copy, "⌘C must trigger a copy action when there is a selection"



class TestTerminalPreferences:
    """Terminal preferences are stored in the preferences API."""

    def test_save_terminal_prefs(self, client):
        """Terminal prefs saved via /api/preferences."""
        payload = {
            "name": "TestUser",
            "work_week": [1, 2, 3, 4, 5],
            "enabled_providers": ["copilot"],
            "theme": "corporate",
            "terminal": {
                "externalTerminal": "iterm",
                "shell": "/bin/zsh",
                "fontSize": 14,
                "scrollback": 10000,
                "customCommand": "",
            },
        }
        res = client.post(
            "/api/preferences",
            data=json.dumps(payload),
            content_type="application/json",
        )
        assert res.status_code == 200
        data = res.get_json()
        assert data["terminal"]["externalTerminal"] == "iterm"
        assert data["terminal"]["shell"] == "/bin/zsh"
        assert data["terminal"]["fontSize"] == 14
        assert data["terminal"]["scrollback"] == 10000

    def test_terminal_prefs_persist(self, client):
        """Terminal prefs persist across reads."""
        payload = {
            "name": "TestUser",
            "work_week": [1, 2, 3, 4, 5],
            "enabled_providers": ["copilot"],
            "theme": "corporate",
            "terminal": {
                "externalTerminal": "warp",
                "shell": "/bin/bash",
                "fontSize": 16,
                "scrollback": 3000,
                "customCommand": "myterm --dir {cwd}",
            },
        }
        client.post(
            "/api/preferences",
            data=json.dumps(payload),
            content_type="application/json",
        )
        res = client.get("/api/preferences")
        assert res.status_code == 200
        data = res.get_json()
        assert data["terminal"]["externalTerminal"] == "warp"
        assert data["terminal"]["customCommand"] == "myterm --dir {cwd}"

    def test_terminal_prefs_defaults(self, client):
        """When no terminal prefs set, should return empty or defaults."""
        res = client.get("/api/preferences")
        assert res.status_code == 200
        data = res.get_json()
        # terminal key may be absent or empty dict
        terminal = data.get("terminal", {})
        assert isinstance(terminal, dict)

    def test_terminal_prefs_update_partial(self, client):
        """Updating terminal prefs doesn't lose other prefs."""
        # First save with some prefs
        payload1 = {
            "name": "Ali",
            "work_week": [1, 2, 3],
            "enabled_providers": ["copilot", "cline"],
            "theme": "starcraft",
            "terminal": {
                "externalTerminal": "auto",
                "shell": "auto",
                "fontSize": 13,
                "scrollback": 5000,
                "customCommand": "",
            },
        }
        client.post(
            "/api/preferences",
            data=json.dumps(payload1),
            content_type="application/json",
        )
        # Update with different terminal prefs
        payload2 = {
            "name": "Ali",
            "work_week": [1, 2, 3],
            "enabled_providers": ["copilot", "cline"],
            "theme": "starcraft",
            "terminal": {
                "externalTerminal": "kitty",
                "shell": "/usr/local/bin/fish",
                "fontSize": 18,
                "scrollback": 50000,
                "customCommand": "",
            },
        }
        res = client.post(
            "/api/preferences",
            data=json.dumps(payload2),
            content_type="application/json",
        )
        data = res.get_json()
        # Terminal prefs updated
        assert data["terminal"]["externalTerminal"] == "kitty"
        assert data["terminal"]["fontSize"] == 18
        # Non-terminal prefs preserved
        assert data["name"] == "Ali"
        assert data["theme"] == "starcraft"

    def test_all_external_terminal_options(self, client):
        """Each external terminal option is accepted."""
        for term in ["auto", "iterm", "terminal", "warp", "alacritty", "kitty", "custom"]:
            payload = {
                "name": "Test",
                "work_week": [1],
                "enabled_providers": ["copilot"],
                "theme": "corporate",
                "terminal": {
                    "externalTerminal": term,
                    "shell": "auto",
                    "fontSize": 13,
                    "scrollback": 5000,
                    "customCommand": "/bin/test --dir {cwd}" if term == "custom" else "",
                },
            }
            res = client.post(
                "/api/preferences",
                data=json.dumps(payload),
                content_type="application/json",
            )
            assert res.status_code == 200
            data = res.get_json()
            assert data["terminal"]["externalTerminal"] == term


# ---------------------------------------------------------------------------
# Terminal bridge module tests
# ---------------------------------------------------------------------------

BRIDGE_JS = os.path.join(REPO, "savant", "static", "js", "terminal-bridge.js")
INDEX_HTML = os.path.join(REPO, "savant", "templates", "index.html")
DETAIL_HTML = os.path.join(REPO, "savant", "templates", "detail.html")


class TestTerminalBridgeModule:
    """Verify the shared terminal-bridge.js module exists and has correct API."""

    def test_bridge_file_exists(self):
        assert os.path.isfile(BRIDGE_JS), "terminal-bridge.js should exist"

    def test_bridge_exports_setup_function(self):
        src = _read(BRIDGE_JS)
        assert "function setupTerminalBridge" in src

    def test_bridge_accepts_options(self):
        src = _read(BRIDGE_JS)
        for opt in ["containerSelector", "fullWidth", "defaultWidthPct",
                     "getSessionCwd", "onDrawerToggle"]:
            assert opt in src, f"Bridge should accept {opt} option"

    def test_bridge_overrides_all_window_functions(self):
        src = _read(BRIDGE_JS)
        expected = ["toggleTerminalDrawer", "termCollapse", "termRestore",
                    "termClose", "termAddTab", "termCloseTab", "termOpenExternal"]
        for fn in expected:
            assert f"window.{fn}" in src, f"Bridge should set window.{fn}"

    def test_bridge_split_only_when_not_fullwidth(self):
        src = _read(BRIDGE_JS)
        assert "if (!fullWidth)" in src, "Split functions should be conditional on !fullWidth"
        assert "termSplitH" in src
        assert "termSplitV" in src
        assert "termMaximize" in src

    def test_bridge_has_drawer_state_sync(self):
        src = _read(BRIDGE_JS)
        assert "_applyDrawerState" in src
        assert "_syncMargin" in src
        assert "onDrawerState" in src

    def test_bridge_has_keyboard_shortcuts(self):
        src = _read(BRIDGE_JS)
        assert "keydown" in src
        assert "addNewTab" in src

    def test_bridge_has_term_indicator_update(self):
        src = _read(BRIDGE_JS)
        assert "_updateTermIndicator" in src
        assert "term-indicator" in src

    def test_bridge_removes_legacy_drawer(self):
        src = _read(BRIDGE_JS)
        assert "terminal-drawer" in src, "Bridge should remove legacy drawer"


class TestTerminalBridgeIntegration:
    """Verify index.html and detail.html import and call the shared bridge."""

    def test_index_imports_bridge(self):
        src = _read(INDEX_HTML)
        assert "terminal-bridge.js" in src, "index.html should import terminal-bridge.js"

    def test_index_calls_setup(self):
        src = _read(INDEX_HTML)
        assert "setupTerminalBridge(" in src, "index.html should call setupTerminalBridge()"

    def test_detail_imports_bridge(self):
        src = _read(DETAIL_HTML)
        assert "terminal-bridge.js" in src, "detail.html should import terminal-bridge.js"

    def test_detail_calls_setup(self):
        src = _read(DETAIL_HTML)
        assert "setupTerminalBridge(" in src, "detail.html should call setupTerminalBridge()"

    def test_detail_uses_fullwidth_option(self):
        src = _read(DETAIL_HTML)
        assert "fullWidth: true" in src or "fullWidth:true" in src, \
            "detail.html should pass fullWidth: true"

    def test_no_duplicate_setup_persistent_terminal(self):
        """After refactor, neither page should have inline _setupPersistentTerminal."""
        idx = _read(INDEX_HTML)
        dtl = _read(DETAIL_HTML)
        idx_iife = len(re.findall(r'\(function\s+_setupPersistentTerminal', idx))
        dtl_iife = len(re.findall(r'\(function\s+_setupPersistentTerminal', dtl))
        assert idx_iife == 0, f"index.html should not have inline _setupPersistentTerminal IIFE (found {idx_iife})"
        assert dtl_iife == 0, f"detail.html should not have inline _setupPersistentTerminal IIFE (found {dtl_iife})"
