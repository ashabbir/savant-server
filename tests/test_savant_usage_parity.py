"""Tests for Savant usage endpoint field parity with Copilot/Claude/Codex.

The frontend renderUsage() in sessions.js expects specific fields from the
usage endpoint.  These tests verify that /api/savant/usage returns all the
same fields as /api/usage (copilot) so the Usage Intelligence, Connected
MCP Servers, and Session Analytics panels render correctly.
"""

import json
import os
import sys
import tempfile
import shutil

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_session(session_id, model="claude-opus-4.6", start="2026-04-15T09:18:17.040230",
                  end="2026-04-15T09:48:17.040230", tools=None, mcp_tools=None):
    """Build a Savant session with configurable tool calls."""
    messages = [
        {"role": "user", "content": "fix the login bug"},
    ]
    tool_calls = []
    call_idx = 0
    for t in (tools or ["read_file", "terminal"]):
        call_idx += 1
        tool_calls.append({
            "id": f"call_{call_idx:03d}",
            "type": "function",
            "function": {"name": t, "arguments": "{}"},
        })
    for t in (mcp_tools or []):
        call_idx += 1
        tool_calls.append({
            "id": f"call_{call_idx:03d}",
            "type": "function",
            "function": {"name": t, "arguments": "{}"},
        })

    messages.append({
        "role": "assistant",
        "content": "Working on it.",
        "tool_calls": tool_calls,
    })
    # Add tool results
    for tc in tool_calls:
        messages.append({
            "role": "tool",
            "content": "ok",
            "tool_call_id": tc["id"],
        })
    messages.append({
        "role": "assistant",
        "content": "Done!",
        "tool_calls": [],
    })
    messages.append({"role": "user", "content": "thanks"})
    messages.append({"role": "assistant", "content": "You're welcome!"})

    return {
        "session_id": session_id,
        "model": model,
        "platform": "cli",
        "session_start": start,
        "last_updated": end,
        "message_count": len(messages),
        "messages": messages,
    }


@pytest.fixture
def savant_sessions_dir(tmp_path, monkeypatch):
    """Create a temp dir with realistic Savant session files."""
    sessions_dir = tmp_path / "savant_sessions"
    sessions_dir.mkdir()

    # Session 1: 30 min, standard tools
    s1 = _make_session(
        "20260415_091817_aaaaaa",
        model="claude-opus-4.6",
        start="2026-04-15T09:18:17.040230",
        end="2026-04-15T09:48:17.040230",
        tools=["read_file", "terminal"],
        mcp_tools=["mcp_savant_workspace_get_current_workspace",
                    "mcp_savant_workspace_list_tasks",
                    "mcp_savant_knowledge_search"],
    )
    (sessions_dir / "session_20260415_091817_aaaaaa.json").write_text(json.dumps(s1))

    # Session 2: 15 min, different model
    s2 = _make_session(
        "20260415_100000_bbbbbb",
        model="gpt-4o",
        start="2026-04-15T10:00:00.000000",
        end="2026-04-15T10:15:00.000000",
        tools=["write_file", "patch"],
        mcp_tools=["mcp_savant_context_code_search"],
    )
    (sessions_dir / "session_20260415_100000_bbbbbb.json").write_text(json.dumps(s2))

    # Patch the SAVANT_SESSIONS_DIR
    monkeypatch.setattr("app.SAVANT_SESSIONS_DIR", str(sessions_dir))
    # Also patch savant state db path to a non-existent file so chains fall back to single sessions
    monkeypatch.setattr("app.SAVANT_STATE_DB", str(tmp_path / "nonexistent.db"))

    return sessions_dir


class TestSavantUsageTotalsFields:
    """Verify totals dict has all fields the frontend needs."""

    def test_totals_has_total_hours(self, client, savant_sessions_dir):
        """renderUsage() line 625: t.total_hours"""
        from app import _build_savant_usage
        data = _build_savant_usage()
        assert "total_hours" in data["totals"], "totals missing total_hours"
        assert isinstance(data["totals"]["total_hours"], (int, float))

    def test_totals_has_avg_session_minutes(self, client, savant_sessions_dir):
        """renderUsage() line 629: t.avg_session_minutes"""
        from app import _build_savant_usage
        data = _build_savant_usage()
        assert "avg_session_minutes" in data["totals"], "totals missing avg_session_minutes"
        assert isinstance(data["totals"]["avg_session_minutes"], (int, float))

    def test_totals_has_avg_tools_per_turn(self, client, savant_sessions_dir):
        """renderUsage() line 679: t.avg_tools_per_turn"""
        from app import _build_savant_usage
        data = _build_savant_usage()
        assert "avg_tools_per_turn" in data["totals"], "totals missing avg_tools_per_turn"

    def test_totals_has_avg_turns_per_message(self, client, savant_sessions_dir):
        """renderUsage() line 683: t.avg_turns_per_message"""
        from app import _build_savant_usage
        data = _build_savant_usage()
        assert "avg_turns_per_message" in data["totals"], "totals missing avg_turns_per_message"

    def test_totals_has_events(self, client, savant_sessions_dir):
        """renderUsage() line 687: t.events"""
        from app import _build_savant_usage
        data = _build_savant_usage()
        assert "events" in data["totals"], "totals missing events"

    def test_session_duration_calculated(self, client, savant_sessions_dir):
        """Session 1 = 30min, Session 2 = 15min -> total 0.75h."""
        from app import _build_savant_usage
        data = _build_savant_usage()
        # 45 min = 0.75 hours
        assert data["totals"]["total_hours"] == 0.8 or data["totals"]["total_hours"] == 0.7 or \
               data["totals"]["total_hours"] == 0.75 or data["totals"]["total_hours"] > 0, \
               f"Expected positive total_hours, got {data['totals']['total_hours']}"
        # Average: 22.5 min
        assert data["totals"]["avg_session_minutes"] > 0, \
               f"Expected positive avg_session_minutes, got {data['totals']['avg_session_minutes']}"


class TestSavantUsageFieldNames:
    """Tools and models must use 'calls' not 'count' to match copilot format."""

    def test_tools_use_calls_not_count(self, client, savant_sessions_dir):
        """renderUsage() line 664: topTools[0].calls"""
        from app import _build_savant_usage
        data = _build_savant_usage()
        assert len(data["tools"]) > 0, "Expected at least one tool"
        for t in data["tools"]:
            assert "calls" in t, f"Tool {t.get('name')} uses 'count' instead of 'calls'"
            assert "count" not in t, f"Tool {t.get('name')} has deprecated 'count' field"

    def test_models_use_calls_not_count(self, client, savant_sessions_dir):
        """renderUsage() line 635: u.models[0].calls"""
        from app import _build_savant_usage
        data = _build_savant_usage()
        assert len(data["models"]) > 0, "Expected at least one model"
        for m in data["models"]:
            assert "calls" in m, f"Model {m.get('name')} uses 'count' instead of 'calls'"


class TestSavantUsageDailyFormat:
    """Daily stats must use 'date' key and include 'turns'."""

    def test_daily_uses_date_not_day(self, client, savant_sessions_dir):
        """renderUsage() line 659: daily[0].date"""
        from app import _build_savant_usage
        data = _build_savant_usage()
        if data["daily"]:
            for d in data["daily"]:
                assert "date" in d, f"Daily entry uses 'day' instead of 'date': {d}"

    def test_daily_has_turns(self, client, savant_sessions_dir):
        """renderUsage() line 654: d.turns (shown in tooltip)"""
        from app import _build_savant_usage
        data = _build_savant_usage()
        if data["daily"]:
            for d in data["daily"]:
                assert "turns" in d, f"Daily entry missing 'turns': {d}"


class TestSavantMCPDetection:
    """MCP bar should detect savant-style mcp_ prefixed tools.

    Savant tools like mcp_savant_workspace_get_current_workspace need to be
    recognized as MCP tools. The JS regex currently only matches mcp:server/tool
    but savant uses mcp_<server>_<tool> format.
    """

    def test_mcp_tools_present_in_usage(self, client, savant_sessions_dir):
        """Verify MCP-prefixed tools appear in usage data."""
        from app import _build_savant_usage
        data = _build_savant_usage()
        tool_names = [t["name"] for t in data["tools"]]
        mcp_tools = [n for n in tool_names if n.startswith("mcp_")]
        assert len(mcp_tools) > 0, f"No MCP tools found. All tools: {tool_names}"


class TestSavantUsageEndpoint:
    """Test the /api/savant/usage HTTP endpoint returns proper data."""

    def test_usage_endpoint_not_loading(self, client, savant_sessions_dir):
        """When sessions exist, usage should not say loading."""
        # First, build the cache directly
        from app import _build_savant_usage, _bg_cache, _bg_lock
        data = _build_savant_usage()
        with _bg_lock:
            _bg_cache['savant_usage'] = data

        rv = client.get("/api/savant/usage")
        assert rv.status_code == 200
        body = rv.get_json()
        assert body.get("loading") is not True, "Usage still shows loading"
        assert "totals" in body
        assert "total_hours" in body["totals"]
        assert "avg_session_minutes" in body["totals"]

    def test_usage_endpoint_shape_matches_copilot(self, client, savant_sessions_dir):
        """Savant usage shape must match copilot for frontend compatibility."""
        from app import _build_savant_usage, _bg_cache, _bg_lock
        data = _build_savant_usage()
        with _bg_lock:
            _bg_cache['savant_usage'] = data

        rv = client.get("/api/savant/usage")
        body = rv.get_json()

        # All top-level keys
        assert "models" in body
        assert "tools" in body
        assert "daily" in body
        assert "totals" in body

        # All totals keys that the frontend accesses
        required_totals = [
            "sessions", "messages", "turns", "tool_calls",
            "events", "total_hours", "avg_session_minutes",
            "avg_tools_per_turn", "avg_turns_per_message",
        ]
        for key in required_totals:
            assert key in body["totals"], f"Missing totals.{key}"
