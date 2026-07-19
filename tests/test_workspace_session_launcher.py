import os
import json
import pytest
from pathlib import Path

UI_EXISTS = Path("savant/templates/index.html").exists()


def test_savant_seed_marker_auto_assigns_workspace(client, sample_workspace, tmp_path, monkeypatch):
    import app as app_module

    savant_root = tmp_path / "savant"
    sessions_dir = savant_root / "sessions"
    meta_dir = savant_root / ".savant-meta"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(app_module, "SAVANT_DIR", str(savant_root))
    monkeypatch.setattr(app_module, "SAVANT_SESSIONS_DIR", str(sessions_dir))
    monkeypatch.setattr(app_module, "SAVANT_META_DIR", str(meta_dir))
    monkeypatch.setattr(app_module, "SAVANT_STATE_DB", str(savant_root / "state.db"))

    sid = "sess_launch_1"
    seed = f"Implement feature kickoff [[SAVANT:WS={sample_workspace};NAME=Launch Session]]"
    payload = {
        "model": "test-model",
        "platform": "cli",
        "session_start": "2026-04-18T00:00:00+00:00",
        "last_updated": "2026-04-18T00:00:10+00:00",
        "messages": [
            {"role": "user", "content": seed}
        ],
    }
    (sessions_dir / f"session_{sid}.json").write_text(json.dumps(payload), encoding="utf-8")

    res = client.get("/api/savant/sessions")
    assert res.status_code == 200
    data = res.get_json()
    sessions = data.get("sessions", [])
    assert sessions, "Expected at least one Savant session"

    s = sessions[0]
    assert s["id"] == sid
    assert s.get("workspace") == sample_workspace
    assert "SAVANT:WS=" not in (s.get("summary") or "")
    assert s.get("summary") == "Launch Session"
    assert "SAVANT:WS=" not in (s.get("last_intent") or "")
    assert (s.get("last_intent") or "").startswith("Implement feature kickoff")


@pytest.mark.skipif(not UI_EXISTS, reason="Client UI files located in savant-client repository")
def test_workspace_new_session_ui_hooks_exist():
    index_html = Path("savant/templates/index.html").read_text(encoding="utf-8")
    ws_js = Path("savant/static/js/workspaces.js").read_text(encoding="utf-8")

    assert 'id="ws-new-session-modal"' in index_html
    assert 'id="ws-new-session-name"' in index_html
    assert 'id="ws-new-session-repo"' in index_html
    assert 'id="ws-new-session-seed"' in index_html
    assert 'id="ws-new-session-provider"' in index_html
    assert 'id="ws-new-session-persona"' in index_html
    assert 'id="ws-new-session-tags"' in index_html
    assert 'id="ws-new-session-tag-pills"' in index_html
    assert 'id="ws-new-session-system-prompt-toggle"' in index_html

    assert "openWsNewSessionModal" in ws_js
    assert "browseWsNewSessionRepo" in ws_js
    assert "startWsNewSession" in ws_js
    assert "_getEnabledSessionProviders" in ws_js
    assert "_providerLaunchCommand" in ws_js
    assert "_providerFollowupDelayMs" in ws_js
    assert "_repoNameFromPath" in ws_js
    assert "_normalizeAbilityTags" in ws_js
    assert "_renderWsNewSessionTagPills" in ws_js
    assert "_bindWsNewSessionTagInput" in ws_js
    assert "_suggestUniqueSessionName" in ws_js
    assert "_sessionNameExistsInWorkspace" in ws_js
    assert "systemPromptToggle.checked = true;" in ws_js
    assert "Session name already exists in this workspace. Try:" in ws_js


@pytest.mark.skipif(not UI_EXISTS, reason="Client UI files located in savant-client repository")
def test_workspace_savant_launcher_uses_one_shot_query_command():
    ws_js = Path("savant/static/js/workspaces.js").read_text(encoding="utf-8")

    assert "savant --yolo chat -q" in ws_js


@pytest.mark.skipif(not UI_EXISTS, reason="Client UI files located in savant-client repository")
def test_workspace_session_launcher_allows_empty_seed_for_any_provider():
    ws_js = Path("savant/static/js/workspaces.js").read_text(encoding="utf-8")

    assert "Seed prompt is required for this provider." not in ws_js
    assert "const followupPrompt = (provider === 'savant' || provider === 'gemini' || provider === 'copilot') ? '' : seedPrompt;" in ws_js


@pytest.mark.skipif(not UI_EXISTS, reason="Client UI files located in savant-client repository")
def test_workspace_savant_launcher_reopens_continue_after_query():
    ws_js = Path("savant/static/js/workspaces.js").read_text(encoding="utf-8")

    assert "savant --yolo --continue" in ws_js


@pytest.mark.skipif(not UI_EXISTS, reason="Client UI files located in savant-client repository")
def test_workspace_gemini_launch_uses_inline_prompt_flag():
    ws_js = Path("savant/static/js/workspaces.js").read_text(encoding="utf-8")

    assert "(provider === 'gemini' && seedPrompt)" in ws_js
    assert "`gemini -i ${_shellSingleQuote(seedPrompt)}`" in ws_js
    assert "const followupPrompt = (provider === 'savant' || provider === 'gemini' || provider === 'copilot') ? '' : seedPrompt;" in ws_js


@pytest.mark.skipif(not UI_EXISTS, reason="Client UI files located in savant-client repository")
def test_workspace_codex_followup_uses_provider_delay():
    ws_js = Path("savant/static/js/workspaces.js").read_text(encoding="utf-8")

    assert "codex: 2200" in ws_js


@pytest.mark.skipif(not UI_EXISTS, reason="Client UI files located in savant-client repository")
def test_workspace_copilot_launch_uses_inline_prompt_flag():
    ws_js = Path("savant/static/js/workspaces.js").read_text(encoding="utf-8")

    assert "(provider === 'copilot' && seedPrompt)" in ws_js
    assert "`copilot -p ${_shellSingleQuote(seedPrompt)}`" in ws_js
    assert "const followupPrompt = (provider === 'savant' || provider === 'gemini' || provider === 'copilot') ? '' : seedPrompt;" in ws_js


@pytest.mark.skipif(not UI_EXISTS, reason="Client UI files located in savant-client repository")
def test_workspace_followup_injection_targets_created_tab_not_current_focus():
    term_html = Path("terminal.html").read_text(encoding="utf-8")

    assert "const targetTabId = created.tabId;" in term_html
    assert "_termApi.write(targetTabId, followup + ENTER);" in term_html
    assert "_termApi.write(latestLeaf.tabId, followup + ENTER);" not in term_html


@pytest.mark.skipif(not UI_EXISTS, reason="Client UI files located in savant-client repository")
def test_workspace_savant_launcher_composes_full_safe_command():
    ws_js = Path("savant/static/js/workspaces.js").read_text(encoding="utf-8")

    assert "const launchCommand = provider === 'savant'" in ws_js
    assert "const setupParts = [" in ws_js
    assert "const combinedPrompt = setupParts.join('\\n\\n');" in ws_js
    assert "attach session to workspace ${workspaceName} then resolve ${persona} abilities" in ws_js
    assert "for repo ${repoName}" in ws_js
    assert "with tags ${tagsForPrompt}" in ws_js
    assert "`savant --yolo chat -q ${_shellSingleQuote(combinedPrompt)} && savant --yolo --continue`" in ws_js
    assert "setupPrompt" not in ws_js
    assert "seedWithMarker" not in ws_js
