"""Tests for enriched Savant session fields — activity_buckets, model_call_counts,
tool_call_counts, checkpoint_count, disk_size, file_count, resume_command,
first/last_event_time, last_event_type, last_intent, active_tools, has_abort,
notes, jira_tickets, mrs, branch, git_root, etc.

These fields match what Copilot/Claude already provide so the frontend
renders Savant session cards identically.
"""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _ts(offset_seconds=0):
    """Generate ISO timestamp with offset from a fixed base time."""
    # Use a fixed base: 2026-04-15T09:00:00Z
    base = 1776258000 + offset_seconds
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(base, tz=timezone.utc)
    return dt.isoformat()


def _make_session(session_id, model="claude-opus-4.6", start_offset=0, msg_count=6):
    """Build a Savant session with richer messages for testing."""
    messages = [
        {"role": "user", "content": "fix the login bug in auth.py"},
        {
            "role": "assistant",
            "content": "Let me check the auth module.",
            "finish_reason": "tool_calls",
            "tool_calls": [
                {
                    "id": "call_001",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": "/tmp/project/auth.py"}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "content": "def login(user):\n    return True",
            "tool_call_id": "call_001",
        },
        {
            "role": "assistant",
            "content": "I see the issue. Let me fix it.",
            "finish_reason": "tool_calls",
            "tool_calls": [
                {
                    "id": "call_002",
                    "type": "function",
                    "function": {
                        "name": "patch",
                        "arguments": json.dumps({
                            "path": "/tmp/project/auth.py",
                            "old_string": "return True",
                            "new_string": "return validate(user)",
                        }),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "content": "Patch applied successfully.",
            "tool_call_id": "call_002",
        },
        {
            "role": "assistant",
            "content": "Fixed! The login function now validates the user properly.",
            "finish_reason": "stop",
            "tool_calls": [],
        },
    ]

    return {
        "session_id": session_id,
        "model": model,
        "base_url": "https://api.githubcopilot.com",
        "platform": "cli",
        "session_start": _ts(start_offset),
        "last_updated": _ts(start_offset + 120),
        "system_prompt": "You are a helpful assistant.",
        "tools": [
            {"type": "function", "function": {"name": "read_file"}},
            {"type": "function", "function": {"name": "patch"}},
            {"type": "function", "function": {"name": "terminal"}},
        ],
        "message_count": len(messages),
        "messages": messages[:msg_count],
    }


def _make_state_db(db_path, sessions_info):
    """Create a state.db with given session rows.

    sessions_info: list of dicts with keys:
      id, parent_session_id, model, started_at, ended_at, end_reason,
      message_count, tool_call_count, title,
      input_tokens, output_tokens
    """
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            source TEXT DEFAULT 'cli',
            user_id TEXT,
            model TEXT,
            model_config TEXT,
            system_prompt TEXT,
            parent_session_id TEXT,
            started_at REAL,
            ended_at REAL,
            end_reason TEXT,
            message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0,
            billing_provider TEXT,
            billing_base_url TEXT,
            billing_mode TEXT,
            estimated_cost_usd REAL DEFAULT 0.0,
            actual_cost_usd REAL,
            cost_status TEXT DEFAULT 'unknown',
            cost_source TEXT DEFAULT 'none',
            pricing_version TEXT,
            title TEXT
        )
    """)
    for s in sessions_info:
        conn.execute(
            "INSERT INTO sessions (id, parent_session_id, model, started_at, ended_at, "
            "end_reason, message_count, tool_call_count, title, input_tokens, output_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                s["id"],
                s.get("parent_session_id"),
                s.get("model", "claude-opus-4.6"),
                s.get("started_at", 1776258000),
                s.get("ended_at"),
                s.get("end_reason"),
                s.get("message_count", 6),
                s.get("tool_call_count", 2),
                s.get("title"),
                s.get("input_tokens", 1000),
                s.get("output_tokens", 500),
            ),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def enriched_savant(tmp_path, monkeypatch):
    
    hdir = tmp_path / "savant"
    sessions_dir = hdir / "sessions"
    sessions_dir.mkdir(parents=True)
    meta_dir = hdir / ".savant-meta"
    meta_dir.mkdir(parents=True)

    root_id = "20260415_090000_aaa111"
    child_id = "20260415_091000_bbb222"

    # Root session — 6 messages
    root_data = _make_session(root_id, model="claude-opus-4.6", start_offset=0)
    (sessions_dir / f"session_{root_id}.json").write_text(json.dumps(root_data))

    # Child session (checkpoint) — different model, more messages
    child_data = _make_session(child_id, model="claude-sonnet-4", start_offset=600)
    child_data["messages"].append({
        "role": "user", "content": "now add unit tests for the fix"
    })
    child_data["messages"].append({
        "role": "assistant",
        "content": "I'll create tests.",
        "finish_reason": "tool_calls",
        "tool_calls": [{
            "id": "call_003",
            "type": "function",
            "function": {
                "name": "terminal",
                "arguments": json.dumps({"command": "pytest tests/"}),
            },
        }],
    })
    child_data["messages"].append({
        "role": "tool",
        "content": "3 passed",
        "tool_call_id": "call_003",
    })
    child_data["messages"].append({
        "role": "assistant",
        "content": "All tests pass!",
        "finish_reason": "stop",
        "tool_calls": [],
    })
    child_data["message_count"] = len(child_data["messages"])
    (sessions_dir / f"session_{child_id}.json").write_text(json.dumps(child_data))

    # State DB with chain
    db_path = str(hdir / "state.db")
    _make_state_db(db_path, [
        {
            "id": root_id,
            "parent_session_id": None,
            "model": "claude-opus-4.6",
            "started_at": 1776258000,
            "ended_at": 1776258600,
            "end_reason": "compression",
            "message_count": 6,
            "tool_call_count": 2,
            "title": "Fix login bug",
            "input_tokens": 5000,
            "output_tokens": 1200,
        },
        {
            "id": child_id,
            "parent_session_id": root_id,
            "model": "claude-sonnet-4",
            "started_at": 1776258600,
            "ended_at": None,
            "end_reason": None,
            "message_count": 10,
            "tool_call_count": 3,
            "title": "Fix login bug",
            "input_tokens": 8000,
            "output_tokens": 2000,
        },
    ])

    # Meta with notes, jira, mrs
    meta = {
        "workspace": "ws-enrich",
        "starred": True,
        "archived": False,
        "nickname": "Auth Fix Session",
        "notes": [
            {"text": "Need to check edge cases", "timestamp": _ts(0)},
            {"text": "All good now", "timestamp": _ts(120)},
        ],
        "jira_tickets": ["APPSERV-1234"],
        "mrs": ["mr_abc123"],
    }
    (meta_dir / f"{root_id}.json").write_text(json.dumps(meta))

    import app as app_mod
    monkeypatch.setattr(app_mod, "SAVANT_DIR", str(hdir))
    monkeypatch.setattr(app_mod, "SAVANT_SESSIONS_DIR", str(sessions_dir))
    monkeypatch.setattr(app_mod, "SAVANT_META_DIR", str(meta_dir))
    monkeypatch.setattr(app_mod, "SAVANT_STATE_DB", db_path)
    app_mod._bg_cache["savant_sessions"] = None
    app_mod._bg_cache["savant_usage"] = None

    return {
        "root_id": root_id,
        "child_id": child_id,
        "sessions_dir": str(sessions_dir),
        "meta_dir": str(meta_dir),
        "hdir": str(hdir),
    }


@pytest.fixture
def enriched_client(_isolated_db, enriched_savant):
    from app import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# ── List endpoint enrichment ────────────────────────────────────────────────


class TestSavantListEnrichedFields:
    """Test that savant_get_all_sessions returns all fields the frontend needs."""

    def test_model_call_counts_in_list(self, enriched_client, enriched_savant):
        """model_call_counts should be populated in list (not just detail)."""
        resp = enriched_client.get("/api/savant/sessions")
        s = resp.get_json()["sessions"][0]
        assert "model_call_counts" in s
        mcc = s["model_call_counts"]
        # We have assistant messages from both root (opus) and child (sonnet)
        assert isinstance(mcc, dict)
        assert len(mcc) > 0

    def test_tool_call_counts_in_list(self, enriched_client, enriched_savant):
        """tool_call_counts should be populated in list."""
        resp = enriched_client.get("/api/savant/sessions")
        s = resp.get_json()["sessions"][0]
        assert "tool_call_counts" in s
        tcc = s["tool_call_counts"]
        assert isinstance(tcc, dict)
        # read_file, patch from root; terminal from child
        assert "read_file" in tcc
        assert "patch" in tcc
        assert "terminal" in tcc

    def test_activity_buckets_in_list(self, enriched_client, enriched_savant):
        """activity_buckets should be a list of 24 ints for the sparkline."""
        resp = enriched_client.get("/api/savant/sessions")
        s = resp.get_json()["sessions"][0]
        assert "activity_buckets" in s
        buckets = s["activity_buckets"]
        assert isinstance(buckets, list)
        # Should be 24 buckets (or empty if timestamps not available)
        if buckets:
            assert len(buckets) == 24
            assert all(isinstance(b, int) for b in buckets)

    def test_checkpoint_count_in_list(self, enriched_client, enriched_savant):
        """checkpoint_count: number of child sessions in the chain."""
        resp = enriched_client.get("/api/savant/sessions")
        s = resp.get_json()["sessions"][0]
        assert "checkpoint_count" in s
        # Chain has root + 1 child = 1 checkpoint
        assert s["checkpoint_count"] == 1

    def test_disk_size_in_list(self, enriched_client, enriched_savant):
        """disk_size should be > 0 for sessions with JSON files."""
        resp = enriched_client.get("/api/savant/sessions")
        s = resp.get_json()["sessions"][0]
        assert "disk_size" in s
        assert isinstance(s["disk_size"], int)
        assert s["disk_size"] > 0

    def test_resume_command_in_list(self, enriched_client, enriched_savant):
        """resume_command should be a savant resume string."""
        resp = enriched_client.get("/api/savant/sessions")
        s = resp.get_json()["sessions"][0]
        assert "resume_command" in s
        rc = s["resume_command"]
        assert "savant" in rc
        assert enriched_savant["root_id"] in rc

    def test_first_last_event_times_in_list(self, enriched_client, enriched_savant):
        """first_event_time and last_event_time from root start / tip end."""
        resp = enriched_client.get("/api/savant/sessions")
        s = resp.get_json()["sessions"][0]
        assert "first_event_time" in s
        assert "last_event_time" in s
        assert s["first_event_time"] is not None
        assert s["last_event_time"] is not None

    def test_last_event_type_in_list(self, enriched_client, enriched_savant):
        """last_event_type should indicate last message role."""
        resp = enriched_client.get("/api/savant/sessions")
        s = resp.get_json()["sessions"][0]
        assert "last_event_type" in s
        # The last message in the chain is an assistant "stop" message
        assert s["last_event_type"] is not None

    def test_notes_jira_mrs_in_list(self, enriched_client, enriched_savant):
        """notes, jira_tickets, mrs should come through from meta."""
        resp = enriched_client.get("/api/savant/sessions")
        s = resp.get_json()["sessions"][0]
        assert "notes" in s
        assert isinstance(s["notes"], list)
        assert len(s["notes"]) == 2
        assert "jira_tickets" in s
        assert s["jira_tickets"] == ["APPSERV-1234"]
        assert "mrs" in s
        # mrs may be enriched (resolved) or raw
        assert isinstance(s["mrs"], list)

    def test_has_abort_in_list(self, enriched_client, enriched_savant):
        """has_abort should default to False when no abort occurred."""
        resp = enriched_client.get("/api/savant/sessions")
        s = resp.get_json()["sessions"][0]
        assert "has_abort" in s
        assert s["has_abort"] is False

    def test_active_tools_in_list(self, enriched_client, enriched_savant):
        """active_tools should be an empty list for completed sessions."""
        resp = enriched_client.get("/api/savant/sessions")
        s = resp.get_json()["sessions"][0]
        assert "active_tools" in s
        assert isinstance(s["active_tools"], list)

    def test_last_intent_in_list(self, enriched_client, enriched_savant):
        """last_intent should be present (may be None)."""
        resp = enriched_client.get("/api/savant/sessions")
        s = resp.get_json()["sessions"][0]
        assert "last_intent" in s

    def test_title_from_state_db(self, enriched_client, enriched_savant):
        """If state.db has a title, it should be used as summary fallback."""
        resp = enriched_client.get("/api/savant/sessions")
        s = resp.get_json()["sessions"][0]
        # Nickname takes precedence, but title should be available
        # nickname "Auth Fix Session" is set in meta
        assert s["summary"] == "Auth Fix Session"


# ── Detail endpoint enrichment ──────────────────────────────────────────────


class TestSavantDetailEnrichedFields:
    """Test that savant_get_session_detail returns all fields the frontend needs."""

    def test_detail_has_activity_buckets(self, enriched_client, enriched_savant):
        resp = enriched_client.get(f"/api/savant/session/{enriched_savant['root_id']}")
        assert resp.status_code == 200
        s = resp.get_json()
        assert "activity_buckets" in s
        if s["activity_buckets"]:
            assert len(s["activity_buckets"]) == 24

    def test_detail_has_checkpoint_count(self, enriched_client, enriched_savant):
        resp = enriched_client.get(f"/api/savant/session/{enriched_savant['root_id']}")
        s = resp.get_json()
        assert "checkpoint_count" in s
        assert s["checkpoint_count"] == 1

    def test_detail_has_disk_size(self, enriched_client, enriched_savant):
        resp = enriched_client.get(f"/api/savant/session/{enriched_savant['root_id']}")
        s = resp.get_json()
        assert "disk_size" in s
        assert s["disk_size"] > 0

    def test_detail_has_resume_command(self, enriched_client, enriched_savant):
        resp = enriched_client.get(f"/api/savant/session/{enriched_savant['root_id']}")
        s = resp.get_json()
        assert "resume_command" in s
        assert "savant" in s["resume_command"]

    def test_detail_has_first_last_event_time(self, enriched_client, enriched_savant):
        resp = enriched_client.get(f"/api/savant/session/{enriched_savant['root_id']}")
        s = resp.get_json()
        assert s["first_event_time"] is not None
        assert s["last_event_time"] is not None

    def test_detail_has_last_event_type(self, enriched_client, enriched_savant):
        resp = enriched_client.get(f"/api/savant/session/{enriched_savant['root_id']}")
        s = resp.get_json()
        assert "last_event_type" in s

    def test_detail_has_notes_jira_mrs(self, enriched_client, enriched_savant):
        resp = enriched_client.get(f"/api/savant/session/{enriched_savant['root_id']}")
        s = resp.get_json()
        assert len(s["notes"]) == 2
        assert s["jira_tickets"] == ["APPSERV-1234"]
        assert isinstance(s["mrs"], list)

    def test_detail_has_has_abort(self, enriched_client, enriched_savant):
        resp = enriched_client.get(f"/api/savant/session/{enriched_savant['root_id']}")
        s = resp.get_json()
        assert s["has_abort"] is False

    def test_detail_has_active_tools(self, enriched_client, enriched_savant):
        resp = enriched_client.get(f"/api/savant/session/{enriched_savant['root_id']}")
        s = resp.get_json()
        assert "active_tools" in s
        assert isinstance(s["active_tools"], list)

    def test_detail_has_last_intent(self, enriched_client, enriched_savant):
        resp = enriched_client.get(f"/api/savant/session/{enriched_savant['root_id']}")
        s = resp.get_json()
        assert "last_intent" in s

    def test_detail_model_call_counts(self, enriched_client, enriched_savant):
        """model_call_counts in detail should show both models."""
        resp = enriched_client.get(f"/api/savant/session/{enriched_savant['root_id']}")
        s = resp.get_json()
        mcc = s["model_call_counts"]
        assert isinstance(mcc, dict)
        assert len(mcc) > 0

    def test_detail_tool_call_counts(self, enriched_client, enriched_savant):
        """tool_call_counts in detail should include all tools."""
        resp = enriched_client.get(f"/api/savant/session/{enriched_savant['root_id']}")
        s = resp.get_json()
        tcc = s["tool_call_counts"]
        assert "read_file" in tcc
        assert "patch" in tcc
        assert "terminal" in tcc

    def test_detail_token_usage(self, enriched_client, enriched_savant):
        """Token usage should be aggregated from state.db."""
        resp = enriched_client.get(f"/api/savant/session/{enriched_savant['root_id']}")
        s = resp.get_json()
        assert "input_tokens" in s
        assert "output_tokens" in s
        # Root: 5000+8000 = 13000 input, 1200+2000 = 3200 output
        assert s["input_tokens"] == 13000
        assert s["output_tokens"] == 3200

    def test_detail_cost_info(self, enriched_client, enriched_savant):
        """Cost info should come from state.db."""
        resp = enriched_client.get(f"/api/savant/session/{enriched_savant['root_id']}")
        s = resp.get_json()
        assert "estimated_cost_usd" in s


# ── Standalone session (no state.db) enrichment ─────────────────────────────


class TestSavantStandaloneEnriched:
    """Test enriched fields work even without state.db (fallback mode)."""

    @pytest.fixture
    def standalone_savant(self, tmp_path, monkeypatch):
        hdir = tmp_path / "savant_standalone"
        sessions_dir = hdir / "sessions"
        sessions_dir.mkdir(parents=True)
        meta_dir = hdir / ".savant-meta"
        meta_dir.mkdir(parents=True)

        sid = "20260415_100000_ccc333"
        data = _make_session(sid, start_offset=3600)
        (sessions_dir / f"session_{sid}.json").write_text(json.dumps(data))
        (meta_dir / f"{sid}.json").write_text(json.dumps({
            "workspace": None, "starred": False, "archived": False,
        }))

        import app as app_mod
        monkeypatch.setattr(app_mod, "SAVANT_DIR", str(hdir))
        monkeypatch.setattr(app_mod, "SAVANT_SESSIONS_DIR", str(sessions_dir))
        monkeypatch.setattr(app_mod, "SAVANT_META_DIR", str(meta_dir))
        # Nonexistent state.db forces fallback
        monkeypatch.setattr(app_mod, "SAVANT_STATE_DB", str(hdir / "nonexistent.db"))
        app_mod._bg_cache["savant_sessions"] = None
        app_mod._bg_cache["savant_usage"] = None
        return sid

    @pytest.fixture
    def standalone_client(self, _isolated_db, standalone_savant):
        from app import app
        app.config["TESTING"] = True
        with app.test_client() as c:
            yield c

    def test_standalone_has_enriched_fields(self, standalone_client, standalone_savant):
        """Even without state.db, all enriched fields should be present."""
        resp = standalone_client.get("/api/savant/sessions")
        s = resp.get_json()["sessions"][0]

        # All these fields must exist (even if empty/zero/None)
        required_fields = [
            "activity_buckets", "model_call_counts", "tool_call_counts",
            "checkpoint_count", "disk_size", "resume_command",
            "first_event_time", "last_event_time", "last_event_type",
            "last_intent", "active_tools", "has_abort",
            "notes", "jira_tickets", "mrs",
        ]
        for f in required_fields:
            assert f in s, f"Missing field: {f}"

    def test_standalone_checkpoint_count_zero(self, standalone_client, standalone_savant):
        """Standalone session has 0 checkpoints."""
        resp = standalone_client.get("/api/savant/sessions")
        s = resp.get_json()["sessions"][0]
        assert s["checkpoint_count"] == 0

    def test_standalone_detail_enriched(self, standalone_client, standalone_savant):
        """Detail endpoint also has enriched fields in standalone mode."""
        resp = standalone_client.get(f"/api/savant/session/{standalone_savant}")
        s = resp.get_json()
        required_fields = [
            "activity_buckets", "checkpoint_count", "disk_size",
            "resume_command", "first_event_time", "last_event_time",
            "last_event_type", "notes", "jira_tickets", "mrs",
            "has_abort", "active_tools", "last_intent",
            "input_tokens", "output_tokens",
        ]
        for f in required_fields:
            assert f in s, f"Missing field in detail: {f}"
