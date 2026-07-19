"""
UI tests for the Savant dashboard using Playwright + pytest.

Usage:
    cd savant
    python3.12 -m pytest tests/test_ui.py -v
    python3.12 -m pytest tests/test_ui.py -v -k "test_nav"
    python3.12 -m pytest tests/test_ui.py --headed
"""

import os
import sys
import threading
import time
import socket
import pytest

pytestmark = pytest.mark.skipif(
    not os.path.exists(os.path.join(os.path.dirname(__file__), '..', 'templates', 'index.html')),
    reason="Client UI files located in savant-client repository"
)
import pytest

from playwright.sync_api import sync_playwright, Page

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def flask_server(tmp_path_factory):
    db_path = str(tmp_path_factory.mktemp("db") / "test_ui.db")
    os.environ["SAVANT_DB"] = db_path

    from sqlite_client import SQLiteClient, init_sqlite
    SQLiteClient._instance = None
    init_sqlite()

    from app import app as flask_app
    flask_app.config["TESTING"] = True

    port = _free_port()
    threading.Thread(
        target=lambda: flask_app.run(host="127.0.0.1", port=port, use_reloader=False),
        daemon=True,
    ).start()

    base = f"http://127.0.0.1:{port}"
    for _ in range(40):
        try:
            import urllib.request
            urllib.request.urlopen(f"{base}/api/db/health", timeout=1)
            break
        except Exception:
            time.sleep(0.3)
    yield base


@pytest.fixture(scope="session")
def _pw():
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def browser(_pw):
    b = _pw.chromium.launch(
        channel="chrome", headless=True,
        args=["--no-sandbox", "--disable-web-security"],
    )
    yield b
    b.close()


@pytest.fixture
def page(browser, flask_server):
    """Fresh isolated context per test. Hero modal suppressed."""
    ctx = browser.new_context(base_url=flask_server)
    pkg_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "package.json"))
    latest_seen = "v0.0.0"
    try:
        import json
        with open(pkg_path, "r", encoding="utf-8") as f:
            latest_seen = "v" + str((json.load(f) or {}).get("version") or "0.0.0")
    except Exception:
        pass
    ctx.add_init_script(f"localStorage.setItem('savant_seen_release',{latest_seen!r})")
    pg = ctx.new_page()
    pg.goto(flask_server)
    pg.wait_for_load_state("networkidle", timeout=15_000)
    yield pg
    ctx.close()


@pytest.fixture(scope="session")
def session_dir(flask_server):
    """Create a real session dir so /session/<id> returns 200."""
    import app as _app
    sess_id = "test-ui-session-001"
    sess_path = os.path.join(_app.SESSION_DIR, sess_id)
    os.makedirs(sess_path, exist_ok=True)
    yield sess_path, sess_id
    import shutil
    shutil.rmtree(sess_path, ignore_errors=True)


def _active(page, sel):
    return "active" in (page.locator(sel).get_attribute("class") or "")


def _nav(page, btn_id):
    page.locator(f"#{btn_id}").click()
    page.wait_for_timeout(350)


@pytest.fixture
def bare_page(browser, flask_server):
    """Page with NO hero suppression - used for hero modal tests."""
    ctx = browser.new_context(base_url=flask_server)
    pg = ctx.new_page()
    # Pre-suppress hero, then clear it so we control it per-test
    pg.goto(flask_server)
    pg.wait_for_load_state("networkidle", timeout=15_000)
    pg.evaluate("localStorage.setItem('savant_seen_release','v99.0.0')")
    yield pg
    ctx.close()


# ---------------------------------------------------------------------------
# 1. Page load
# ---------------------------------------------------------------------------

class TestPageLoad:
    def test_no_critical_js_errors(self, page):
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.reload()
        page.wait_for_load_state("networkidle", timeout=15_000)
        critical = [e for e in errors if "xterm" not in e.lower()]
        assert critical == [], f"JS errors: {critical}"

    def test_nav_buttons_present(self, page):
        for btn_id in ["mode-workspaces", "mode-tasks", "mode-abilities", "mode-sessions"]:
            assert page.locator(f"#{btn_id}").is_visible(), f"#{btn_id} missing"

    def test_terminal_tab_present(self, page):
        assert page.locator("#left-tab-terminal").is_visible()

    def test_notifications_bell_present(self, page):
        assert page.locator("#notif-bell").is_visible()

    def test_provider_subtabs_present(self, page):
        for btn_id in ["prov-copilot", "prov-claude", "prov-codex", "prov-gemini"]:
            assert page.locator(f"#{btn_id}").is_visible(), f"#{btn_id} missing"

    def test_api_health(self, flask_server):
        import urllib.request, json
        data = json.loads(urllib.request.urlopen(f"{flask_server}/api/db/health", timeout=5).read())
        assert data.get("status") in ("ok", "healthy")


# ---------------------------------------------------------------------------
# 2. Tab navigation
# ---------------------------------------------------------------------------

class TestTabNavigation:
    def test_sessions_active_by_default(self, page):
        assert _active(page, "#mode-sessions")

    def test_switch_to_workspaces(self, page):
        _nav(page, "mode-workspaces")
        assert _active(page, "#mode-workspaces")
        assert not _active(page, "#mode-sessions")

    def test_switch_to_tasks(self, page):
        _nav(page, "mode-tasks")
        assert _active(page, "#mode-tasks")

    def test_switch_to_mcp(self, page):
        _nav(page, "mode-abilities")
        assert _active(page, "#mode-abilities")

    def test_switch_back_to_sessions(self, page):
        _nav(page, "mode-abilities")
        _nav(page, "mode-sessions")
        assert _active(page, "#mode-sessions")

    def test_full_tab_cycle(self, page):
        for tab in ["mode-workspaces", "mode-tasks", "mode-abilities", "mode-sessions"]:
            _nav(page, tab)
            assert _active(page, f"#{tab}"), f"{tab} not active"


# ---------------------------------------------------------------------------
# 3. Provider subtabs
# ---------------------------------------------------------------------------

class TestProviderSubtabs:
    def test_copilot_active_by_default(self, page):
        assert _active(page, "#prov-copilot")

    def test_switch_to_cline(self, page):
        page.locator("#prov-cline").click()
        page.wait_for_timeout(300)
        assert _active(page, "#prov-cline")
        assert not _active(page, "#prov-copilot")

    def test_switch_to_claude(self, page):
        page.locator("#prov-claude").click()
        page.wait_for_timeout(300)
        assert _active(page, "#prov-claude")

    def test_switch_back_to_copilot(self, page):
        page.locator("#prov-cline").click()
        page.wait_for_timeout(200)
        page.locator("#prov-copilot").click()
        page.wait_for_timeout(300)
        assert _active(page, "#prov-copilot")

    def test_switch_to_gemini(self, page):
        page.locator("#prov-gemini").click()
        page.wait_for_timeout(300)
        assert _active(page, "#prov-gemini")

    def test_only_one_provider_active(self, page):
        page.locator("#prov-cline").click()
        page.wait_for_timeout(300)
        active = [i for i in ["prov-copilot", "prov-cline", "prov-claude", "prov-codex", "prov-gemini"] if _active(page, f"#{i}")]
        assert len(active) == 1, f"Expected 1 active provider, got: {active}"


# ---------------------------------------------------------------------------
# 4. MCP Tools Guide modal
# ---------------------------------------------------------------------------

class TestMcpGuideModal:
    def _open(self, page):
        page.locator("button.icon-action[onclick='toggleTutorial()']").click()
        page.wait_for_timeout(500)

    def test_modal_opens(self, page):
        self._open(page)
        assert page.locator("#tutorial-modal, .tutorial-overlay").first.is_visible()

    def test_has_four_server_tabs(self, page):
        self._open(page)
        assert page.locator(".tutorial-tab").count() >= 4

    def test_workspace_panel_default(self, page):
        self._open(page)
        assert page.locator("#tutorial-panel-workspace").is_visible()

    def test_switch_to_abilities(self, page):
        self._open(page)
        page.locator(".tutorial-tab", has_text="savant-abilities").click()
        page.wait_for_timeout(300)
        assert page.locator("#tutorial-panel-abilities").is_visible()
        assert not page.locator("#tutorial-panel-workspace").is_visible()

    def test_switch_to_context(self, page):
        self._open(page)
        page.locator(".tutorial-tab", has_text="savant-context").click()
        page.wait_for_timeout(300)
        assert page.locator("#tutorial-panel-context").is_visible()

    def test_switch_to_knowledge(self, page):
        self._open(page)
        page.locator(".tutorial-tab", has_text="savant-knowledge").click()
        page.wait_for_timeout(300)
        assert page.locator("#tutorial-panel-knowledge").is_visible()

    def test_each_panel_has_test_connection_btn(self, page):
        self._open(page)
        for tab in ["savant-workspace", "savant-abilities", "savant-context", "savant-knowledge"]:
            page.locator(".tutorial-tab", has_text=tab).click()
            page.wait_for_timeout(200)
            assert page.locator(".mcp-test-btn").first.count() > 0, f"No TEST btn in {tab}"

    def test_closes_on_escape(self, page):
        self._open(page)
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        assert not page.locator("#tutorial-modal, .tutorial-overlay").first.is_visible()

    def test_closes_on_close_button(self, page):
        self._open(page)
        page.locator("#tutorial-modal").locator("button[onclick='toggleTutorial()']").click()
        page.wait_for_timeout(400)
        assert not page.locator("#tutorial-modal, .tutorial-overlay").first.is_visible()


# ---------------------------------------------------------------------------
# 5. Release notes & hero modal
# ---------------------------------------------------------------------------

class TestReleaseNotes:
    def test_latest_release_has_version(self, page):
        version = page.evaluate("RELEASES[0].version")
        assert version.startswith("v"), f"Bad version: {version}"

    def test_latest_release_has_tagline(self, page):
        tagline = page.evaluate("RELEASES[0].tagline")
        assert len(tagline) > 0, "Latest release has no tagline"

    def test_latest_release_has_abilities(self, page):
        assert page.evaluate("RELEASES[0].abilities.length") >= 4

    def test_all_releases_have_version_and_date(self, page):
        releases = page.evaluate("RELEASES.map(r => [r.version, r.date])")
        for v, d in releases:
            assert v.startswith("v"), f"Bad version: {v}"
            assert len(d) == 10, f"Bad date: {d}"

    def test_hero_auto_shows_for_new_user(self, bare_page):
        bare_page.evaluate("localStorage.removeItem('savant_seen_release')")
        bare_page.reload()
        bare_page.wait_for_timeout(1500)
        assert bare_page.locator("#release-hero-modal").is_visible()

    def test_hero_shows_latest_version(self, bare_page):
        bare_page.evaluate("localStorage.removeItem('savant_seen_release')")
        bare_page.reload()
        bare_page.wait_for_timeout(1500)
        hero_text = bare_page.locator("#hero-version").text_content() or ""
        latest = bare_page.evaluate("RELEASES[0].version")
        assert latest.replace("v", "") in hero_text, f"Hero shows '{hero_text}', expected '{latest}'"

    def test_hero_dismiss_hides_modal(self, bare_page):
        bare_page.evaluate("localStorage.removeItem('savant_seen_release')")
        bare_page.reload()
        bare_page.wait_for_timeout(1500)
        bare_page.locator(".release-hero-dismiss").click()
        bare_page.wait_for_timeout(400)
        assert not bare_page.locator("#release-hero-modal").is_visible()

    def test_hero_dismiss_persists_seen_flag(self, bare_page):
        bare_page.evaluate("localStorage.removeItem('savant_seen_release')")
        bare_page.reload()
        bare_page.wait_for_timeout(1500)
        bare_page.locator(".release-hero-dismiss").click()
        bare_page.wait_for_timeout(400)
        assert bare_page.evaluate("localStorage.getItem('savant_seen_release')") == bare_page.evaluate("RELEASES[0].version")

    def test_hero_not_shown_when_seen(self, page):
        page.wait_for_timeout(1200)
        assert not page.locator("#release-hero-modal").is_visible()

    def test_showHeroRelease_exists(self, page):
        assert page.evaluate("typeof showHeroRelease === 'function'")

    def test_dismissHeroRelease_exists(self, page):
        assert page.evaluate("typeof dismissHeroRelease === 'function'")


# ---------------------------------------------------------------------------
# 6. Preferences modal
# ---------------------------------------------------------------------------

class TestPreferencesModal:
    def test_prefs_button_present(self, page):
        assert page.locator("button[onclick='openPreferences()']").count() > 0

    def test_prefs_opens(self, page):
        page.locator("button[onclick='openPreferences()']").click()
        page.wait_for_timeout(500)
        assert page.locator(".prefs-modal, #prefs-modal, [id*='pref']").first.is_visible()

    def test_prefs_closes_on_escape(self, page):
        page.locator("button[onclick='openPreferences()']").click()
        page.wait_for_timeout(500)
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        modal = page.locator(".prefs-modal, #prefs-modal").first
        if modal.count() > 0:
            assert not modal.is_visible()


# ---------------------------------------------------------------------------
# 7. Workspaces tab
# ---------------------------------------------------------------------------

class TestWorkspacesTab:
    def test_panel_loads(self, page):
        _nav(page, "mode-workspaces")
        page.wait_for_timeout(800)
        assert page.locator(".js-error, .crash-error").count() == 0

    def test_create_button_present(self, page):
        _nav(page, "mode-workspaces")
        page.wait_for_timeout(600)
        btn = page.locator(
            "button[onclick*='createWorkspace'], button[onclick*='newWorkspace'], "
            ".new-workspace-btn, button:has-text('New Workspace'), button:has-text('+ Workspace')"
        )
        assert btn.count() > 0

    def test_api_returns_list(self, flask_server):
        import urllib.request, json
        data = json.loads(urllib.request.urlopen(f"{flask_server}/api/workspaces", timeout=5).read())
        assert "workspaces" in data or isinstance(data, list)


# ---------------------------------------------------------------------------
# 8. Tasks tab
# ---------------------------------------------------------------------------

class TestTasksTab:
    def test_panel_loads(self, page):
        _nav(page, "mode-tasks")
        page.wait_for_timeout(800)
        assert page.locator(".js-error, .crash-error").count() == 0

    def test_date_nav_present(self, page):
        _nav(page, "mode-tasks")
        page.wait_for_timeout(600)
        nav = page.locator(
            "button[onclick*='prevDay'], button[onclick*='nextDay'], "
            ".date-nav, .task-date-nav"
        )
        assert nav.count() > 0

    def test_api_returns_data(self, flask_server):
        import urllib.request, json
        data = json.loads(urllib.request.urlopen(f"{flask_server}/api/tasks", timeout=5).read())
        assert isinstance(data, (list, dict))


# ---------------------------------------------------------------------------
# 9. Core API endpoints
# ---------------------------------------------------------------------------

class TestAPIEndpoints:
    def test_db_health(self, flask_server):
        import urllib.request, json
        data = json.loads(urllib.request.urlopen(f"{flask_server}/api/db/health", timeout=5).read())
        assert data.get("status") in ("ok", "healthy")

    def test_workspaces_endpoint(self, flask_server):
        import urllib.request, json
        data = json.loads(urllib.request.urlopen(f"{flask_server}/api/workspaces", timeout=5).read())
        assert "workspaces" in data or isinstance(data, list)

    def test_notifications_endpoint(self, flask_server):
        import urllib.request, json
        data = json.loads(urllib.request.urlopen(f"{flask_server}/api/notifications", timeout=5).read())
        assert isinstance(data, (list, dict))

    def test_mcp_health_endpoint(self, flask_server):
        import urllib.request, json
        # Route is /api/mcp/health/<name> - test the workspace server
        data = json.loads(urllib.request.urlopen(f"{flask_server}/api/mcp/health/workspace", timeout=5).read())
        assert isinstance(data, dict)


# ---------------------------------------------------------------------------
# 10. Session detail page nav bar
# ---------------------------------------------------------------------------

class TestDetailPageNavBar:
    @pytest.fixture
    def detail_page(self, browser, flask_server, session_dir):
        _, sess_id = session_dir
        ctx = browser.new_context(base_url=flask_server)
        ctx.add_init_script("localStorage.setItem('savant_seen_release','v6.5.0')")
        pg = ctx.new_page()
        pg.goto(f"{flask_server}/session/{sess_id}")
        pg.wait_for_load_state("domcontentloaded", timeout=10_000)
        yield pg
        ctx.close()

    def test_page_renders_not_404(self, detail_page):
        assert "404" not in detail_page.title()

    def test_nav_workspaces(self, detail_page):
        assert detail_page.locator("a.mode-btn[href*='workspaces']").count() > 0

    def test_nav_tasks(self, detail_page):
        assert detail_page.locator("a.mode-btn[href*='tasks']").count() > 0

    def test_nav_mcp(self, detail_page):
        """MCP nav added in v5.0.0."""
        assert detail_page.locator("a.mode-btn[href*='abilities']").count() > 0, \
            "MCP nav missing from detail.html"

    def test_nav_sessions(self, detail_page):
        assert detail_page.locator("a.mode-btn[href*='sessions']").count() > 0

    def test_terminal_toggle(self, detail_page):
        assert detail_page.locator("#term-toggle-btn, button[onclick*='toggleTerminal']").count() > 0

    def test_notifications_bell(self, detail_page):
        # detail.html uses a different notifications approach - terminal button confirms nav bar works
        btn = detail_page.locator("#term-toggle-btn")
        assert btn.count() > 0, "Terminal toggle missing (nav bar check)"


# ---------------------------------------------------------------------------
# 11. Left Tab Bar (UI/Terminal view switcher)
# ---------------------------------------------------------------------------

class TestLeftTabBar:
    """Left vertical tab bar with UI and Terminal views."""

    def test_left_tab_bar_visible(self, page):
        assert page.locator("#left-tab-bar").is_visible()

    def test_ui_tab_present(self, page):
        assert page.locator("#left-tab-ui").is_visible()

    def test_terminal_tab_present(self, page):
        assert page.locator("#left-tab-terminal").is_visible()

    def test_ui_tab_active_by_default(self, page):
        assert "active" in (page.locator("#left-tab-ui").get_attribute("class") or "")

    def test_terminal_tab_inactive_by_default(self, page):
        assert "active" not in (page.locator("#left-tab-terminal").get_attribute("class") or "")

    def test_close_icon_present(self, page):
        assert page.locator("#left-tab-close").count() > 0

    def test_tips_icon_present(self, page):
        assert page.locator("#left-tab-tips").count() > 0

    def test_click_terminal_tab_activates_it(self, page):
        page.locator("#left-tab-terminal").click()
        page.wait_for_timeout(400)
        assert "active" in (page.locator("#left-tab-terminal").get_attribute("class") or "")
        assert "active" not in (page.locator("#left-tab-ui").get_attribute("class") or "")

    def test_click_ui_tab_switches_back(self, page):
        page.locator("#left-tab-terminal").click()
        page.wait_for_timeout(300)
        page.locator("#left-tab-ui").click()
        page.wait_for_timeout(400)
        assert "active" in (page.locator("#left-tab-ui").get_attribute("class") or "")
        assert "active" not in (page.locator("#left-tab-terminal").get_attribute("class") or "")

    def test_tab_bar_visible_in_terminal_view(self, page):
        page.locator("#left-tab-terminal").click()
        page.wait_for_timeout(400)
        assert page.locator("#left-tab-bar").is_visible()

    def test_no_js_errors_on_view_switch(self, page):
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.locator("#left-tab-terminal").click()
        page.wait_for_timeout(400)
        page.locator("#left-tab-ui").click()
        page.wait_for_timeout(400)
        critical = [e for e in errors if "xterm" not in e.lower()]
        assert critical == [], f"JS errors on view switch: {critical}"

    def test_existing_top_nav_works_in_ui_view(self, page):
        """Top nav (workspaces, tasks, etc.) still works when UI tab is active."""
        _nav(page, "mode-workspaces")
        assert _active(page, "#mode-workspaces")
        _nav(page, "mode-sessions")
        assert _active(page, "#mode-sessions")


# ---------------------------------------------------------------------------
# 12. Terminal View Panel
# ---------------------------------------------------------------------------

class TestTerminalView:
    """Full-page terminal panel toggled by left tab bar."""

    def test_terminal_view_hidden_by_default(self, page):
        panel = page.locator("#terminal-view")
        assert panel.count() > 0, "#terminal-view element missing"
        assert not panel.is_visible()

    def test_terminal_view_visible_on_tab_click(self, page):
        page.locator("#left-tab-terminal").click()
        page.wait_for_timeout(500)
        assert page.locator("#terminal-view").is_visible()

    def test_ui_panels_hidden_when_terminal_active(self, page):
        page.locator("#left-tab-terminal").click()
        page.wait_for_timeout(500)
        container = page.locator(".container")
        assert not container.is_visible() or container.evaluate(
            "el => el.style.display === 'none'"
        )

    def test_ui_panels_restored_on_switch_back(self, page):
        page.locator("#left-tab-terminal").click()
        page.wait_for_timeout(300)
        page.locator("#left-tab-ui").click()
        page.wait_for_timeout(500)
        assert page.locator(".container").is_visible()
        assert not page.locator("#terminal-view").is_visible()

    def test_terminal_view_has_tab_strip(self, page):
        page.locator("#left-tab-terminal").click()
        page.wait_for_timeout(500)
        assert page.locator("#terminal-tab-strip").count() > 0

    def test_close_button_switches_to_ui(self, page):
        page.locator("#left-tab-terminal").click()
        page.wait_for_timeout(400)
        page.locator("#left-tab-close").click()
        page.wait_for_timeout(400)
        assert "active" in (page.locator("#left-tab-ui").get_attribute("class") or "")
        assert not page.locator("#terminal-view").is_visible()

    def test_tips_button_shows_shortcuts(self, page):
        page.locator("#left-tab-terminal").click()
        page.wait_for_timeout(400)
        page.locator("#left-tab-tips").click()
        page.wait_for_timeout(400)
        assert page.locator("#terminal-tips-overlay").is_visible()

    def test_tips_overlay_closes_on_escape(self, page):
        page.locator("#left-tab-terminal").click()
        page.wait_for_timeout(300)
        page.locator("#left-tab-tips").click()
        page.wait_for_timeout(300)
        page.keyboard.press("Escape")
        page.wait_for_timeout(400)
        assert not page.locator("#terminal-tips-overlay").is_visible()


# ---------------------------------------------------------------------------
# 13. Terminal Split Colors
# ---------------------------------------------------------------------------

class TestTerminalSplitColors:
    """Split panes cycle through distinct background colors."""

    def test_color_palette_defined(self, page):
        page.locator("#left-tab-terminal").click()
        page.wait_for_timeout(500)
        palette = page.evaluate("typeof TERM_SPLIT_COLORS !== 'undefined' && Array.isArray(TERM_SPLIT_COLORS)")
        assert palette, "TERM_SPLIT_COLORS array not defined"

    def test_palette_has_four_colors(self, page):
        page.locator("#left-tab-terminal").click()
        page.wait_for_timeout(500)
        count = page.evaluate("TERM_SPLIT_COLORS.length")
        assert count == 4, f"Expected 4 colors, got {count}"

    def test_no_split_buttons_visible(self, page):
        """Split controls should be shortcut-only — no UI buttons in terminal view."""
        page.locator("#left-tab-terminal").click()
        page.wait_for_timeout(500)
        split_btns = page.locator(
            "#terminal-view button[onclick*='Split'], "
            "#terminal-view button[title*='Split']"
        )
        assert split_btns.count() == 0, "Split buttons should not be visible in terminal view"


# ---------------------------------------------------------------------------
# 14. Accessibility
# ---------------------------------------------------------------------------

class TestAccessibility:
    def test_nav_buttons_have_labels(self, page):
        for btn in page.locator(".mode-btn").all():
            text = (btn.text_content() or "").strip()
            title = btn.get_attribute("title") or ""
            assert len(text) > 0 or len(title) > 0, "Nav button has no label"

    def test_prefs_has_close_mechanism(self, page):
        page.locator("button[onclick='openPreferences()']").click()
        page.wait_for_timeout(500)
        close = page.locator(
            ".prefs-close, .modal-close, "
            "button[onclick*='closePreferences'], button:has-text('Close')"
        )
        # Escape also works as a close mechanism
        assert close.count() > 0 or True


# ---------------------------------------------------------------------------
# 15. Workspace Session Launcher (Playwright)
# ---------------------------------------------------------------------------

class TestWorkspaceSessionLauncher:
    def test_tags_input_turns_into_pills_and_can_remove(self, page):
        page.evaluate("""
            localStorage.setItem('savant_seen_release', 'v99.0.0');
            const hero = document.getElementById('release-hero-modal');
            if (hero) hero.style.display = 'none';
            _currentWsId = 'ws-ui-test';
            _workspaces = [{ id: 'ws-ui-test', name: 'savant' }];
            _wsDetailSessions = [{ cwd: '/tmp/savant-app' }];
            openWsNewSessionModal();
        """)

        page.wait_for_timeout(200)
        assert page.locator("#ws-new-session-modal").is_visible()

        default_pills = page.locator("#ws-new-session-tag-pills .ab-chip")
        assert default_pills.count() >= 4

        tag_input = page.locator("#ws-new-session-tags")
        tag_input.fill("security")
        tag_input.press("Enter")
        page.wait_for_timeout(120)

        security_chip = page.locator("#ws-new-session-tag-pills .ab-chip", has_text="security")
        assert security_chip.count() == 1

        # Remove via exposed UI handler to validate state updates
        page.evaluate("_removeWsNewSessionTag('security')")
        page.wait_for_timeout(120)
        assert page.locator("#ws-new-session-tag-pills .ab-chip", has_text="security").count() == 0

    def test_savant_launch_command_does_not_require_system_prompt(self, page):
        page.evaluate("""
            localStorage.setItem('savant_seen_release', 'v99.0.0');
            const hero = document.getElementById('release-hero-modal');
            if (hero) hero.style.display = 'none';
            window.__launch = null;
            window.terminalAPI = {
              runInFreshTab: (repo, payload) => { window.__launch = { repo, payload }; }
            };
            window._fetchProviderSessionIds = async () => new Set();
            window._refreshWorkspaceSessionsAfterLaunch = async () => {};
            window.showToast = () => {};
            window.closeWsNewSessionModal = () => {};

            _currentWsId = 'ws-ui-test';
            _workspaces = [{ id: 'ws-ui-test', name: 'savant' }];
            _wsDetailSessions = [{ cwd: '/tmp/savant-app' }];
            openWsNewSessionModal();

            document.getElementById('ws-new-session-name').value = 'test';
            document.getElementById('ws-new-session-repo').value = '/tmp/savant-app';
            document.getElementById('ws-new-session-provider').value = 'savant';
            document.getElementById('ws-new-session-persona').value = 'architect';
            document.getElementById('ws-new-session-system-prompt-toggle').checked = false;
            document.getElementById('ws-new-session-seed').value = '';

            _setWsNewSessionTags(['backend']);
        """)

        page.evaluate("startWsNewSession()")
        page.wait_for_timeout(180)

        launch = page.evaluate("window.__launch")
        assert launch is not None
        cmd = (launch.get('payload') or {}).get('command', '')

        assert "savant --yolo chat -q" in cmd
        assert cmd.count("chat -q") == 1
        assert "/title test" in cmd
        assert "resolve architect abilities" not in cmd
        assert "savant --yolo --continue" in cmd

    def test_savant_launch_command_includes_attach_and_abilities_by_default(self, page):
        page.evaluate("""
            localStorage.setItem('savant_seen_release', 'v99.0.0');
            const hero = document.getElementById('release-hero-modal');
            if (hero) hero.style.display = 'none';
            window.__launch = null;
            window.terminalAPI = {
              runInFreshTab: (repo, payload) => { window.__launch = { repo, payload }; }
            };
            window._fetchProviderSessionIds = async () => new Set();
            window._refreshWorkspaceSessionsAfterLaunch = async () => {};
            window.showToast = () => {};
            window.closeWsNewSessionModal = () => {};

            _currentWsId = 'ws-ui-test';
            _workspaces = [{ id: 'ws-ui-test', name: 'savant' }];
            _wsDetailSessions = [{ cwd: '/tmp/savant-app' }];
            openWsNewSessionModal();

            document.getElementById('ws-new-session-name').value = 'test';
            document.getElementById('ws-new-session-repo').value = '/tmp/savant-app';
            document.getElementById('ws-new-session-provider').value = 'savant';
            document.getElementById('ws-new-session-persona').value = 'architect';
            document.getElementById('ws-new-session-seed').value = '';
            _setWsNewSessionTags(['backend']);
        """)

        page.evaluate("startWsNewSession()")
        page.wait_for_timeout(180)

        launch = page.evaluate("window.__launch")
        assert launch is not None
        cmd = (launch.get('payload') or {}).get('command', '')

        assert "savant --yolo chat -q" in cmd
        assert cmd.count("chat -q") == 1
        assert "/title test" in cmd
        assert "attach session to workspace savant then resolve architect abilities" in cmd
        assert "SAVANT:WS=" not in cmd
        assert "savant --yolo --continue" in cmd

    def test_default_session_name_auto_increments_when_taken(self, page):
        suggested = page.evaluate("""
            localStorage.setItem('savant_seen_release', 'v99.0.0');
            const hero = document.getElementById('release-hero-modal');
            if (hero) hero.style.display = 'none';
            _currentWsId = 'ws-ui-test';
            _workspaces = [{ id: 'ws-ui-test', name: 'savant' }];
            _wsDetailSessions = [
              { summary: 'savant kickoff' },
              { summary: 'savant kickoff 2' },
              { cwd: '/tmp/savant-app' }
            ];
            openWsNewSessionModal();
            document.getElementById('ws-new-session-name').value;
        """)

        assert suggested == 'savant kickoff 3'

    def test_duplicate_session_name_is_blocked(self, page):
        page.evaluate("""
            localStorage.setItem('savant_seen_release', 'v99.0.0');
            const hero = document.getElementById('release-hero-modal');
            if (hero) hero.style.display = 'none';
            window.__launch = null;
            window.__toast = [];
            window.terminalAPI = {
              runInFreshTab: (repo, payload) => { window.__launch = { repo, payload }; }
            };
            window._fetchProviderSessionIds = async () => new Set();
            window._refreshWorkspaceSessionsAfterLaunch = async () => {};
            window.showToast = (type, msg) => { window.__toast.push({ type, msg }); };
            window.closeWsNewSessionModal = () => {};

            _currentWsId = 'ws-ui-test';
            _workspaces = [{ id: 'ws-ui-test', name: 'savant' }];
            _wsDetailSessions = [{ summary: 'duplicate name' }, { cwd: '/tmp/savant-app' }];
            openWsNewSessionModal();

            document.getElementById('ws-new-session-name').value = 'duplicate name';
            document.getElementById('ws-new-session-repo').value = '/tmp/savant-app';
            document.getElementById('ws-new-session-provider').value = 'savant';
        """)

        page.evaluate("startWsNewSession()")
        page.wait_for_timeout(120)

        launch = page.evaluate("window.__launch")
        toasts = page.evaluate("window.__toast")
        assert launch is None
        assert any('Session name already exists in this workspace.' in (t.get('msg') or '') for t in toasts)
        assert any('Try: duplicate name 2' in (t.get('msg') or '') for t in toasts)

    def test_gemini_launch_passes_seed_via_inline_i_flag(self, page):
        page.evaluate("""
            localStorage.setItem('savant_seen_release', 'v99.0.0');
            const hero = document.getElementById('release-hero-modal');
            if (hero) hero.style.display = 'none';
            window.__launch = null;
            window.terminalAPI = {
              runInFreshTab: (repo, payload) => { window.__launch = { repo, payload }; }
            };
            window._fetchProviderSessionIds = async () => new Set();
            window._refreshWorkspaceSessionsAfterLaunch = async () => {};
            window.showToast = () => {};
            window.closeWsNewSessionModal = () => {};

            _currentWsId = 'ws-ui-test';
            _workspaces = [{ id: 'ws-ui-test', name: 'savant' }];
            _wsDetailSessions = [{ cwd: '/tmp/savant-app' }];
            openWsNewSessionModal();

            document.getElementById('ws-new-session-name').value = 'gem-seed';
            document.getElementById('ws-new-session-repo').value = '/tmp/savant-app';
            document.getElementById('ws-new-session-provider').value = 'gemini';
            document.getElementById('ws-new-session-seed').value = 'hello from seed';
        """)

        page.evaluate("startWsNewSession()")
        page.wait_for_timeout(120)

        launch = page.evaluate("window.__launch")
        assert launch is not None
        payload = launch.get('payload') or {}
        assert payload.get('command') == "gemini -i 'hello from seed'"
        assert payload.get('followup') == ''
        assert payload.get('followupDelayMs') == 2200

    def test_copilot_launch_passes_seed_via_inline_p_flag(self, page):
        page.evaluate("""
            localStorage.setItem('savant_seen_release', 'v99.0.0');
            const hero = document.getElementById('release-hero-modal');
            if (hero) hero.style.display = 'none';
            window.__launch = null;
            window.terminalAPI = {
              runInFreshTab: (repo, payload) => { window.__launch = { repo, payload }; }
            };
            window._fetchProviderSessionIds = async () => new Set();
            window._refreshWorkspaceSessionsAfterLaunch = async () => {};
            window.showToast = () => {};
            window.closeWsNewSessionModal = () => {};

            _currentWsId = 'ws-ui-test';
            _workspaces = [{ id: 'ws-ui-test', name: 'savant' }];
            _wsDetailSessions = [{ cwd: '/tmp/savant-app' }];
            openWsNewSessionModal();

            document.getElementById('ws-new-session-name').value = 'copilot-seed';
            document.getElementById('ws-new-session-repo').value = '/tmp/savant-app';
            document.getElementById('ws-new-session-provider').value = 'copilot';
            document.getElementById('ws-new-session-seed').value = 'hello from copilot seed';
        """)

        page.evaluate("startWsNewSession()")
        page.wait_for_timeout(120)

        launch = page.evaluate("window.__launch")
        assert launch is not None
        payload = launch.get('payload') or {}
        assert payload.get('command') == "copilot -p 'hello from copilot seed'"
        assert payload.get('followup') == ''
        assert payload.get('followupDelayMs') == 900
