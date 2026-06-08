

import json
import os
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_savant_session(session_id, model="claude-opus-4.6", messages=None, start=None, updated=None):
    """Build a Savant session JSON payload."""
    start = start or "2026-04-15T09:28:36.000000"
    updated = updated or start
    if messages is None:
        messages = [
            {"role": "user", "content": f"Message for {session_id}"},
            {
                "role": "assistant",
                "content": f"Response for {session_id}",
                "finish_reason": "stop",
                "reasoning": "",
                "tool_calls": [],
            },
        ]
    return {
        "session_id": session_id,
        "model": model,
        "base_url": "https://api.githubcopilot.com",
        "platform": "cli",
        "session_start": start,
        "last_updated": updated,
        "system_prompt": "You are a helpful assistant.",
        "tools": ["terminal", "read_file"],
        "message_count": len(messages),
        "messages": messages,
    }


def _create_state_db(db_path, rows):
    """Create a minimal Savant state.db with the given session rows.

    Each row is a dict with keys: id, parent_session_id, end_reason, message_count, title, model.
    started_at is computed from the id timestamp portion.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL DEFAULT 'cli',
            user_id TEXT,
            model TEXT,
            model_config TEXT,
            system_prompt TEXT,
            parent_session_id TEXT,
            started_at REAL NOT NULL,
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
            estimated_cost_usd REAL,
            actual_cost_usd REAL,
            cost_status TEXT,
            cost_source TEXT,
            pricing_version TEXT,
            title TEXT,
            FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
        )
    """)
    for r in rows:
        conn.execute(
            "INSERT INTO sessions (id, parent_session_id, end_reason, message_count, title, model, started_at, input_tokens, output_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                r["id"],
                r.get("parent_session_id"),
                r.get("end_reason"),
                r.get("message_count", 10),
                r.get("title"),
                r.get("model", "claude-opus-4.6"),
                r.get("started_at", time.time()),
                r.get("input_tokens", 1000),
                r.get("output_tokens", 500),
            ),
        )
    conn.commit()
    conn.close()


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def savant_chain_dir(tmp_path, monkeypatch):
    """Set up Savant directory with a 3-session chain + 1 standalone + stale JSON.

    Chain: root_001 (compression) -> child_002 (compression) -> tip_003 (active)
    Standalone: solo_004 (no parent, no children)
    Stale JSON: stale_005 (has JSON file but NOT in state.db -- should be ignored)
    """
    hdir = tmp_path / "savant"
    sessions_dir = hdir / "sessions"
    sessions_dir.mkdir(parents=True)
    meta_dir = hdir / ".savant-meta"
    meta_dir.mkdir(parents=True)

    # IDs
    root_id = "20260415_092836_root01"
    child_id = "20260415_100248_child2"
    tip_id = "20260415_100952_tip003"
    solo_id = "20260415_091350_solo04"
    stale_id = "20260415_094354_stale5"

    # Create JSON files for all 5
    chain_msgs_root = [
        {"role": "user", "content": "fix the auth bug"},
        {"role": "assistant", "content": "Looking at it now.", "finish_reason": "tool_calls", "reasoning": "",
         "tool_calls": [{"id": "c1", "call_id": "c1", "type": "function",
                         "function": {"name": "read_file", "arguments": json.dumps({"path": "/tmp/auth.py"})}}]},
        {"role": "tool", "content": "def login(): pass", "tool_call_id": "c1"},
        {"role": "assistant", "content": "Found the issue.", "finish_reason": "stop", "reasoning": "", "tool_calls": []},
    ]
    chain_msgs_child = [
        {"role": "user", "content": "[CONTEXT COMPACTION] Previous work: fixed auth bug"},
        {"role": "assistant", "content": "Continuing from checkpoint.", "finish_reason": "tool_calls", "reasoning": "",
         "tool_calls": [{"id": "c2", "call_id": "c2", "type": "function",
                         "function": {"name": "patch", "arguments": json.dumps({"path": "/tmp/auth.py", "old_string": "pass", "new_string": "validate()"})}}]},
        {"role": "tool", "content": "Patched.", "tool_call_id": "c2"},
        {"role": "assistant", "content": "Applied the fix.", "finish_reason": "stop", "reasoning": "", "tool_calls": []},
    ]
    chain_msgs_tip = [
        {"role": "user", "content": "[CONTEXT COMPACTION] Auth bug fixed, testing now"},
        {"role": "assistant", "content": "Running tests.", "finish_reason": "tool_calls", "reasoning": "",
         "tool_calls": [{"id": "c3", "call_id": "c3", "type": "function",
                         "function": {"name": "terminal", "arguments": json.dumps({"command": "pytest tests/"})}}]},
        {"role": "tool", "content": "All tests pass.", "tool_call_id": "c3"},
        {"role": "assistant", "content": "All tests pass! Auth bug is fully fixed.", "finish_reason": "stop", "reasoning": "", "tool_calls": []},
    ]
    solo_msgs = [
        {"role": "user", "content": "list all python files"},
        {"role": "assistant", "content": "Here are the Python files.", "finish_reason": "stop", "reasoning": "", "tool_calls": []},
    ]
    stale_msgs = [
        {"role": "user", "content": "stale session content"},
        {"role": "assistant", "content": "This is stale.", "finish_reason": "stop", "reasoning": "", "tool_calls": []},
    ]

    (sessions_dir / f"session_{root_id}.json").write_text(json.dumps(
        _make_savant_session(root_id, messages=chain_msgs_root, start="2026-04-15T09:28:36.000000", updated="2026-04-15T09:59:00.000000")))
    (sessions_dir / f"session_{child_id}.json").write_text(json.dumps(
        _make_savant_session(child_id, messages=chain_msgs_child, start="2026-04-15T10:02:48.000000", updated="2026-04-15T10:09:00.000000")))
    (sessions_dir / f"session_{tip_id}.json").write_text(json.dumps(
        _make_savant_session(tip_id, messages=chain_msgs_tip, start="2026-04-15T10:09:52.000000", updated="2026-04-15T10:20:00.000000")))
    (sessions_dir / f"session_{solo_id}.json").write_text(json.dumps(
        _make_savant_session(solo_id, messages=solo_msgs, start="2026-04-15T09:13:50.000000", updated="2026-04-15T09:15:00.000000")))
    (sessions_dir / f"session_{stale_id}.json").write_text(json.dumps(
        _make_savant_session(stale_id, messages=stale_msgs, start="2026-04-15T09:43:54.000000", updated="2026-04-15T09:45:00.000000")))

    # Meta for root (applies to entire chain)
    (meta_dir / f"{root_id}.json").write_text(json.dumps({"workspace": "ws-chain", "starred": True, "nickname": "Auth Bug Fix"}))

    # Create state.db -- only root/child/tip/solo are registered (stale is NOT)
    db_path = str(hdir / "state.db")
    _create_state_db(db_path, [
        {"id": root_id, "parent_session_id": None, "end_reason": "compression", "message_count": 4, "title": "auth bug fix", "started_at": 1744706916.0, "input_tokens": 5000, "output_tokens": 2000},
        {"id": child_id, "parent_session_id": root_id, "end_reason": "compression", "message_count": 4, "title": "auth bug fix #2", "started_at": 1744708968.0, "input_tokens": 3000, "output_tokens": 1500},
        {"id": tip_id, "parent_session_id": child_id, "end_reason": None, "message_count": 4, "title": "auth bug fix #3", "started_at": 1744709392.0, "input_tokens": 2000, "output_tokens": 1000},
        {"id": solo_id, "parent_session_id": None, "end_reason": None, "message_count": 2, "title": "list files", "started_at": 1744705630.0, "input_tokens": 500, "output_tokens": 200},
    ])

    monkeypatch.setenv("SAVANT_DIR", str(hdir))
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
        "tip_id": tip_id,
        "solo_id": solo_id,
        "stale_id": stale_id,
        "sessions_dir": str(sessions_dir),
        "meta_dir": str(meta_dir),
        "db_path": db_path,
    }


@pytest.fixture
def chain_client(_isolated_db, savant_chain_dir):
    from app import app

    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# ── Chain building ───────────────────────────────────────────────────────────


def test_build_session_chains(savant_chain_dir):
    """_savant_build_session_chains returns correct root->tip mapping and chain info."""
    import app as app_mod

    chains = app_mod._savant_build_session_chains()

    # Should have 2 logical sessions: the chain (root) and the standalone
    assert len(chains) == 2

    root_id = savant_chain_dir["root_id"]
    solo_id = savant_chain_dir["solo_id"]

    assert root_id in chains
    assert solo_id in chains

    # Chain should point to tip
    chain_info = chains[root_id]
    assert chain_info["tip_id"] == savant_chain_dir["tip_id"]
    assert chain_info["root_id"] == root_id
    assert len(chain_info["chain"]) == 3  # root, child, tip
    assert chain_info["chain"][0] == root_id
    assert chain_info["chain"][-1] == savant_chain_dir["tip_id"]

    # Solo is its own tip
    solo_info = chains[solo_id]
    assert solo_info["tip_id"] == solo_id
    assert solo_info["root_id"] == solo_id
    assert len(solo_info["chain"]) == 1

    # Stale session should NOT appear
    stale_id = savant_chain_dir["stale_id"]
    assert stale_id not in chains


def test_build_session_chains_no_state_db(savant_chain_dir, monkeypatch):
    """When state.db doesn't exist, fall back to treating each JSON as standalone."""
    import app as app_mod

    monkeypatch.setattr(app_mod, "SAVANT_STATE_DB", "/nonexistent/state.db")
    chains = app_mod._savant_build_session_chains()

    # Should have 5 entries — one per JSON file (fallback mode)
    assert len(chains) == 5
    # Each session is its own root and tip
    for sid, info in chains.items():
        assert info["tip_id"] == sid
        assert info["root_id"] == sid
        assert len(info["chain"]) == 1


# ── Session listing (grouped) ───────────────────────────────────────────────


def test_sessions_list_groups_checkpoints(chain_client, savant_chain_dir):
    """GET /api/savant/sessions should show 2 logical sessions, not 5."""
    resp = chain_client.get("/api/savant/sessions")
    assert resp.status_code == 200
    data = resp.get_json()
    sessions = data["sessions"]

    assert data["total"] == 2  # chain + solo, NOT 5
    assert len(sessions) == 2

    ids = {s["id"] for s in sessions}
    # The chain session should use root_id as its canonical ID
    assert savant_chain_dir["root_id"] in ids
    assert savant_chain_dir["solo_id"] in ids

    # Checkpoint IDs should NOT appear as separate entries
    assert savant_chain_dir["child_id"] not in ids
    assert savant_chain_dir["tip_id"] not in ids
    assert savant_chain_dir["stale_id"] not in ids


def test_sessions_list_chain_uses_tip_data(chain_client, savant_chain_dir):
    """The chain entry should use data from the tip session (latest), not root."""
    resp = chain_client.get("/api/savant/sessions")
    sessions = resp.get_json()["sessions"]

    chain_entry = next(s for s in sessions if s["id"] == savant_chain_dir["root_id"])

    # modified should be from the tip's last_updated
    assert chain_entry["modified"] == "2026-04-15T10:20:00.000000"
    # created should be from the root's session_start
    assert chain_entry["created"] == "2026-04-15T09:28:36.000000"


def test_sessions_list_chain_meta_from_root(chain_client, savant_chain_dir):
    """Meta (workspace, starred, nickname) should use root_id."""
    resp = chain_client.get("/api/savant/sessions")
    sessions = resp.get_json()["sessions"]

    chain_entry = next(s for s in sessions if s["id"] == savant_chain_dir["root_id"])
    assert chain_entry["workspace"] == "ws-chain"
    assert chain_entry["starred"] is True
    assert chain_entry["summary"] == "Auth Bug Fix"  # nickname takes precedence


def test_sessions_list_chain_aggregates_tools(chain_client, savant_chain_dir):
    """Tool counts should be aggregated across all sessions in the chain."""
    resp = chain_client.get("/api/savant/sessions")
    sessions = resp.get_json()["sessions"]

    chain_entry = next(s for s in sessions if s["id"] == savant_chain_dir["root_id"])
    # root has read_file, child has patch, tip has terminal = 3 tool calls total
    assert chain_entry["tool_call_count"] == 3
    assert set(chain_entry["tools_used"]) == {"read_file", "patch", "terminal"}


# ── Detail resolution ────────────────────────────────────────────────────────


def test_detail_resolves_root_id(chain_client, savant_chain_dir):
    """GET /api/savant/session/<root_id> should work and show chain data."""
    resp = chain_client.get(f"/api/savant/session/{savant_chain_dir['root_id']}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["id"] == savant_chain_dir["root_id"]


def test_detail_resolves_child_id(chain_client, savant_chain_dir):
    """GET /api/savant/session/<child_id> should resolve to the chain and use root_id."""
    resp = chain_client.get(f"/api/savant/session/{savant_chain_dir['child_id']}")
    assert resp.status_code == 200
    data = resp.get_json()
    # Should resolve to the root of the chain
    assert data["id"] == savant_chain_dir["root_id"]


def test_detail_resolves_tip_id(chain_client, savant_chain_dir):
    """GET /api/savant/session/<tip_id> should resolve to the chain and use root_id."""
    resp = chain_client.get(f"/api/savant/session/{savant_chain_dir['tip_id']}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["id"] == savant_chain_dir["root_id"]


def test_detail_standalone(chain_client, savant_chain_dir):
    """GET /api/savant/session/<solo_id> returns its own ID (no chain)."""
    resp = chain_client.get(f"/api/savant/session/{savant_chain_dir['solo_id']}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["id"] == savant_chain_dir["solo_id"]


def test_detail_chain_aggregated_tools(chain_client, savant_chain_dir):
    """Detail for chain should aggregate tools from all sessions."""
    resp = chain_client.get(f"/api/savant/session/{savant_chain_dir['root_id']}")
    data = resp.get_json()
    assert data["tool_call_count"] == 3
    assert set(data["tools_used"]) == {"read_file", "patch", "terminal"}


def test_detail_chain_dates(chain_client, savant_chain_dir):
    """Detail should use root's created and tip's modified."""
    resp = chain_client.get(f"/api/savant/session/{savant_chain_dir['root_id']}")
    data = resp.get_json()
    assert data["created"] == "2026-04-15T09:28:36.000000"
    assert data["created_at"] == "2026-04-15T09:28:36.000000"
    assert data["modified"] == "2026-04-15T10:20:00.000000"
    assert data["updated_at"] == "2026-04-15T10:20:00.000000"


# ── Conversation merging ─────────────────────────────────────────────────────


def test_conversation_merges_chain(chain_client, savant_chain_dir):
    """Conversation endpoint should merge messages from all sessions in the chain."""
    resp = chain_client.get(f"/api/savant/session/{savant_chain_dir['root_id']}/conversation")
    assert resp.status_code == 200
    data = resp.get_json()

    conv = data["conversation"]
    # Each session has 4 entries: user_message, assistant_message+tool_start, assistant_message
    # New format: user_message, assistant_message, tool_start, assistant_message = 4 per session
    # Total: 3 sessions * 4 = 12
    assert len(conv) == 12

    # First message should be from root
    assert conv[0]["type"] == "user_message"
    assert "fix the auth bug" in conv[0]["content"]

    # Last message should be from tip
    assert conv[-1]["type"] == "assistant_message"
    assert "fully fixed" in conv[-1]["content"]

    # Tool map should have all 3 tool calls
    tools = data["tools"]
    assert len(tools) == 3
    tool_names = {v["name"] for v in tools.values()}
    assert tool_names == {"read_file", "patch", "terminal"}

    # Stats should be aggregated
    stats = data["stats"]
    assert stats["tool_calls"] == 3


def test_conversation_child_id_merges_same(chain_client, savant_chain_dir):
    """Requesting conversation via child_id should return same merged conversation."""
    resp_root = chain_client.get(f"/api/savant/session/{savant_chain_dir['root_id']}/conversation")
    resp_child = chain_client.get(f"/api/savant/session/{savant_chain_dir['child_id']}/conversation")

    assert resp_root.status_code == 200
    assert resp_child.status_code == 200

    conv_root = resp_root.get_json()["conversation"]
    conv_child = resp_child.get_json()["conversation"]
    assert len(conv_root) == len(conv_child)


def test_conversation_standalone(chain_client, savant_chain_dir):
    """Standalone session conversation should just return its own messages."""
    resp = chain_client.get(f"/api/savant/session/{savant_chain_dir['solo_id']}/conversation")
    assert resp.status_code == 200
    conv = resp.get_json()["conversation"]
    assert len(conv) == 2  # user + assistant


# ── Usage (no double-counting) ───────────────────────────────────────────────


def test_usage_no_double_counting(savant_chain_dir):
    """Usage should count only sessions from state.db, not stale JSONs."""
    import app as app_mod

    usage = app_mod._build_savant_usage()
    # 2 logical sessions (chain + solo), not 4 or 5
    assert usage["totals"]["sessions"] == 2

    # Tool calls: 3 from chain + 0 from solo = 3
    assert usage["totals"]["tool_calls"] == 3

    # Input/output tokens from state.db
    # Chain: 5000+3000+2000 = 10000 input, 2000+1500+1000 = 4500 output
    # Solo: 500 input, 200 output
    assert usage["totals"]["input_tokens"] == 10500
    assert usage["totals"]["output_tokens"] == 4700


# ── Delete chain ─────────────────────────────────────────────────────────────


def test_delete_chain_removes_all_files(chain_client, savant_chain_dir):
    """Deleting root_id should remove all JSON files in the chain."""
    root_id = savant_chain_dir["root_id"]
    child_id = savant_chain_dir["child_id"]
    tip_id = savant_chain_dir["tip_id"]
    sessions_dir = savant_chain_dir["sessions_dir"]

    # Confirm all 3 chain files exist
    assert os.path.isfile(os.path.join(sessions_dir, f"session_{root_id}.json"))
    assert os.path.isfile(os.path.join(sessions_dir, f"session_{child_id}.json"))
    assert os.path.isfile(os.path.join(sessions_dir, f"session_{tip_id}.json"))

    resp = chain_client.delete(f"/api/savant/session/{root_id}")
    assert resp.status_code == 200

    # All chain files should be gone
    assert not os.path.isfile(os.path.join(sessions_dir, f"session_{root_id}.json"))
    assert not os.path.isfile(os.path.join(sessions_dir, f"session_{child_id}.json"))
    assert not os.path.isfile(os.path.join(sessions_dir, f"session_{tip_id}.json"))

    # Solo and stale should still exist
    assert os.path.isfile(os.path.join(sessions_dir, f"session_{savant_chain_dir['solo_id']}.json"))


def test_delete_via_child_id_removes_chain(chain_client, savant_chain_dir):
    """Deleting via child_id should resolve to chain and delete all files."""
    child_id = savant_chain_dir["child_id"]

    resp = chain_client.delete(f"/api/savant/session/{child_id}")
    assert resp.status_code == 200

    sessions_dir = savant_chain_dir["sessions_dir"]
    assert not os.path.isfile(os.path.join(sessions_dir, f"session_{savant_chain_dir['root_id']}.json"))
    assert not os.path.isfile(os.path.join(sessions_dir, f"session_{child_id}.json"))
    assert not os.path.isfile(os.path.join(sessions_dir, f"session_{savant_chain_dir['tip_id']}.json"))


# ── Workspace/star/rename apply to root_id ───────────────────────────────────


def test_workspace_assign_via_child(chain_client, savant_chain_dir):
    """Workspace assignment via child_id should apply to root_id meta."""
    child_id = savant_chain_dir["child_id"]
    root_id = savant_chain_dir["root_id"]

    resp = chain_client.post(
        f"/api/savant/session/{child_id}/workspace",
        json={"workspace_id": "ws-new"},
    )
    assert resp.status_code == 200

    # Verify via root_id detail
    detail = chain_client.get(f"/api/savant/session/{root_id}").get_json()
    assert detail["workspace"] == "ws-new"


def test_star_via_tip_id(chain_client, savant_chain_dir):
    """Star toggle via tip_id should apply to root's meta."""
    tip_id = savant_chain_dir["tip_id"]

    # Initially starred via root meta
    resp = chain_client.post(f"/api/savant/session/{tip_id}/star")
    assert resp.status_code == 200
    # Should toggle from True to False
    assert resp.get_json()["starred"] is False


def test_rename_via_child(chain_client, savant_chain_dir):
    """Rename via child_id should apply to root's meta."""
    child_id = savant_chain_dir["child_id"]
    root_id = savant_chain_dir["root_id"]

    resp = chain_client.post(
        f"/api/savant/session/{child_id}/rename",
        json={"nickname": "Renamed Chain"},
    )
    assert resp.status_code == 200

    sessions = chain_client.get("/api/savant/sessions").get_json()["sessions"]
    chain_entry = next(s for s in sessions if s["id"] == root_id)
    assert chain_entry["summary"] == "Renamed Chain"


# ── Project files & git changes resolve chain ────────────────────────────────


def test_project_files_merges_chain(chain_client, savant_chain_dir):
    """Project files should aggregate file operations across chain."""
    resp = chain_client.get(f"/api/savant/session/{savant_chain_dir['root_id']}/project-files")
    assert resp.status_code == 200
    files = resp.get_json()["files"]
    paths = [f["path"] for f in files]
    assert "/tmp/auth.py" in paths


def test_project_files_via_child(chain_client, savant_chain_dir):
    """Project files via child_id should return same chain-aggregated result."""
    resp = chain_client.get(f"/api/savant/session/{savant_chain_dir['child_id']}/project-files")
    assert resp.status_code == 200
    files = resp.get_json()["files"]
    paths = [f["path"] for f in files]
    assert "/tmp/auth.py" in paths
