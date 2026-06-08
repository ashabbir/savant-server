"""Tests for Savant conversation parity with Copilot/Claude format.

The frontend renderConversation() expects entries with `type` field, not `role`.
These tests verify savant_parse_full_conversation() outputs the exact same
shape that Claude/Copilot parsers produce.
"""

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app import savant_parse_full_conversation


# ── Fixtures ──

@pytest.fixture
def savant_session_dir(tmp_path, monkeypatch):
    """Create a temporary Savant sessions directory with a test session."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    session_id = "20260415_120000_aaa111"
    session_data = {
        "model": "claude-opus-4-6",
        "platform": "cli",
        "session_start": "2026-04-15T12:00:00Z",
        "last_updated": "2026-04-15T13:00:00Z",
        "messages": [
            {
                "role": "user",
                "content": "Hello, help me fix a bug"
            },
            {
                "role": "assistant",
                "content": "I'll look into that bug for you.",
                "tool_calls": [
                    {
                        "id": "call_001",
                        "type": "function",
                        "function": {
                            "name": "terminal",
                            "arguments": "{\"command\": \"ls -la\"}"
                        }
                    }
                ]
            },
            {
                "role": "tool",
                "tool_call_id": "call_001",
                "content": "total 42\ndrwxr-xr-x  5 user staff 160 Apr 15 12:00 ."
            },
            {
                "role": "assistant",
                "content": "I can see the directory listing. Let me check the file."
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_002",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": "{\"path\": \"bug.py\"}"
                        }
                    },
                    {
                        "id": "call_003",
                        "type": "function",
                        "function": {
                            "name": "search_files",
                            "arguments": "{\"pattern\": \"error\"}"
                        }
                    }
                ]
            },
            {
                "role": "tool",
                "tool_call_id": "call_002",
                "content": "def buggy():\n    return None"
            },
            {
                "role": "tool",
                "tool_call_id": "call_003",
                "content": "No matches found"
            },
            {
                "role": "assistant",
                "content": "Found the issue. Let me fix it.",
                "tool_calls": [
                    {
                        "id": "call_004",
                        "type": "function",
                        "function": {
                            "name": "patch",
                            "arguments": json.dumps({"path": "bug.py", "old_string": "return None", "new_string": "return True"})
                        }
                    }
                ]
            },
            {
                "role": "tool",
                "tool_call_id": "call_004",
                "content": "Applied patch successfully"
            },
            {
                "role": "user",
                "content": "Great, thanks!"
            },
            {
                "role": "assistant",
                "content": "You're welcome! The bug is fixed."
            }
        ]
    }

    session_file = sessions_dir / f"session_{session_id}.json"
    session_file.write_text(json.dumps(session_data))

    # Monkey-patch the sessions directory
    monkeypatch.setattr("app.SAVANT_SESSIONS_DIR", str(sessions_dir))
    # Disable state.db chain resolution
    monkeypatch.setattr("app._savant_build_session_chains", lambda: {})

    return session_id


# ── Tests: Conversation entry types ──

class TestConversationEntryTypes:
    """Verify conversation entries use `type` field matching Copilot/Claude."""

    def test_user_messages_have_type_user_message(self, savant_session_dir):
        conv, _, _ = savant_parse_full_conversation(savant_session_dir)
        user_entries = [e for e in conv if e.get("type") == "user_message"]
        assert len(user_entries) == 2
        assert user_entries[0]["content"] == "Hello, help me fix a bug"
        assert user_entries[1]["content"] == "Great, thanks!"

    def test_user_messages_have_no_role_field(self, savant_session_dir):
        conv, _, _ = savant_parse_full_conversation(savant_session_dir)
        user_entries = [e for e in conv if e.get("type") == "user_message"]
        for entry in user_entries:
            assert "role" not in entry

    def test_assistant_messages_have_type_assistant_message(self, savant_session_dir):
        conv, _, _ = savant_parse_full_conversation(savant_session_dir)
        asst_entries = [e for e in conv if e.get("type") == "assistant_message"]
        assert len(asst_entries) >= 2  # at least text-only ones

    def test_assistant_has_tool_requests_not_tool_calls(self, savant_session_dir):
        conv, _, _ = savant_parse_full_conversation(savant_session_dir)
        asst_entries = [e for e in conv if e.get("type") == "assistant_message"]
        for entry in asst_entries:
            assert "tool_calls" not in entry
            # tool_requests should be a list (possibly empty)
            assert isinstance(entry.get("tool_requests", []), list)

    def test_tool_calls_become_tool_start_entries(self, savant_session_dir):
        """Each tool_call in an assistant message produces a tool_start entry."""
        conv, _, _ = savant_parse_full_conversation(savant_session_dir)
        tool_starts = [e for e in conv if e.get("type") == "tool_start"]
        # We have 4 tool calls total: call_001, call_002, call_003, call_004
        assert len(tool_starts) == 4

    def test_tool_start_has_call_id_and_tool_name(self, savant_session_dir):
        conv, _, _ = savant_parse_full_conversation(savant_session_dir)
        tool_starts = [e for e in conv if e.get("type") == "tool_start"]
        for ts in tool_starts:
            assert "call_id" in ts
            assert "tool_name" in ts
            assert ts["call_id"]  # non-empty
            assert ts["tool_name"]  # non-empty

    def test_tool_start_names_correct(self, savant_session_dir):
        conv, _, _ = savant_parse_full_conversation(savant_session_dir)
        tool_starts = [e for e in conv if e.get("type") == "tool_start"]
        names = [ts["tool_name"] for ts in tool_starts]
        assert names == ["terminal", "read_file", "search_files", "patch"]

    def test_no_role_field_in_any_entry(self, savant_session_dir):
        """No entry should have a 'role' field — frontend uses 'type'."""
        conv, _, _ = savant_parse_full_conversation(savant_session_dir)
        for entry in conv:
            assert "role" not in entry, f"Entry has 'role' field: {entry}"

    def test_all_entries_have_type_field(self, savant_session_dir):
        conv, _, _ = savant_parse_full_conversation(savant_session_dir)
        valid_types = {"user_message", "assistant_message", "tool_start"}
        for entry in conv:
            assert "type" in entry, f"Entry missing 'type' field: {entry}"
            assert entry["type"] in valid_types, f"Unknown type: {entry['type']}"


# ── Tests: Tool map format ──

class TestToolMap:
    """Verify tool_map matches what renderConversation expects."""

    def test_tool_map_keys_are_call_ids(self, savant_session_dir):
        _, tool_map, _ = savant_parse_full_conversation(savant_session_dir)
        assert "call_001" in tool_map
        assert "call_002" in tool_map
        assert "call_003" in tool_map
        assert "call_004" in tool_map

    def test_tool_map_has_name_args_result(self, savant_session_dir):
        _, tool_map, _ = savant_parse_full_conversation(savant_session_dir)
        for call_id, info in tool_map.items():
            assert "name" in info
            assert "args" in info  # frontend uses 'args' not 'arguments'
            assert "result" in info

    def test_tool_map_results_populated(self, savant_session_dir):
        _, tool_map, _ = savant_parse_full_conversation(savant_session_dir)
        assert tool_map["call_001"]["result"] is not None
        assert "total 42" in tool_map["call_001"]["result"]
        assert tool_map["call_002"]["result"] == "def buggy():\n    return None"
        assert tool_map["call_004"]["result"] == "Applied patch successfully"

    def test_tool_map_has_success_field(self, savant_session_dir):
        """Frontend checks toolInfo.success for status icon."""
        _, tool_map, _ = savant_parse_full_conversation(savant_session_dir)
        for call_id, info in tool_map.items():
            assert "success" in info


# ── Tests: Stats format ──

class TestStatsParity:
    """Verify stats match what renderConvSidebar() expects."""

    def test_stats_has_user_messages(self, savant_session_dir):
        _, _, stats = savant_parse_full_conversation(savant_session_dir)
        assert "user_messages" in stats
        assert stats["user_messages"] == 2

    def test_stats_has_assistant_messages(self, savant_session_dir):
        _, _, stats = savant_parse_full_conversation(savant_session_dir)
        assert "assistant_messages" in stats
        assert stats["assistant_messages"] == 5  # 5 assistant messages total

    def test_stats_has_tool_calls(self, savant_session_dir):
        _, _, stats = savant_parse_full_conversation(savant_session_dir)
        assert "tool_calls" in stats
        assert stats["tool_calls"] == 4

    def test_stats_has_tool_success_rate(self, savant_session_dir):
        _, _, stats = savant_parse_full_conversation(savant_session_dir)
        assert "tool_success_rate" in stats
        assert isinstance(stats["tool_success_rate"], (int, float))

    def test_stats_has_avg_response_length(self, savant_session_dir):
        _, _, stats = savant_parse_full_conversation(savant_session_dir)
        assert "avg_response_length" in stats
        assert isinstance(stats["avg_response_length"], (int, float))

    def test_stats_has_files_created_and_edited(self, savant_session_dir):
        _, _, stats = savant_parse_full_conversation(savant_session_dir)
        assert "files_created" in stats
        assert "files_edited" in stats
        assert isinstance(stats["files_created"], list)
        assert isinstance(stats["files_edited"], list)

    def test_stats_files_edited_detects_patch(self, savant_session_dir):
        """patch tool with 'path' arg should add to files_edited."""
        _, _, stats = savant_parse_full_conversation(savant_session_dir)
        assert "bug.py" in stats["files_edited"]

    def test_stats_no_old_field_names(self, savant_session_dir):
        """Old field names should NOT be present."""
        _, _, stats = savant_parse_full_conversation(savant_session_dir)
        assert "message_count" not in stats
        assert "turn_count" not in stats
        assert "tool_call_count" not in stats
        assert "tools_used" not in stats
        assert "tool_call_counts" not in stats


# ── Tests: Conversation ordering ──

class TestConversationOrdering:
    """Verify entries maintain correct message order."""

    def test_conversation_order_preserved(self, savant_session_dir):
        conv, _, _ = savant_parse_full_conversation(savant_session_dir)
        types = [e["type"] for e in conv]
        # Expected order:
        # user, assistant (with tool_requests), tool_start, assistant, assistant (with tool_requests), tool_start, tool_start, assistant (with tool_requests), tool_start, user, assistant
        assert types[0] == "user_message"
        assert types[1] == "assistant_message"  # "I'll look into that..."
        assert types[2] == "tool_start"          # terminal
        assert types[3] == "assistant_message"  # "I can see the dir..."
        assert types[4] == "assistant_message"  # empty content, tools only
        assert types[5] == "tool_start"          # read_file
        assert types[6] == "tool_start"          # search_files

    def test_timestamps_present(self, savant_session_dir):
        conv, _, _ = savant_parse_full_conversation(savant_session_dir)
        for entry in conv:
            assert "timestamp" in entry


# ── Tests: Checkpoints in session detail ──

class TestSavantCheckpointsInDetail:
    """Verify session detail returns tree with checkpoint entries."""

    def test_detail_has_tree_key(self, savant_session_dir):
        from app import savant_get_session_detail
        info = savant_get_session_detail(savant_session_dir)
        assert "tree" in info

    def test_tree_has_checkpoints_list(self, savant_session_dir):
        from app import savant_get_session_detail
        info = savant_get_session_detail(savant_session_dir)
        tree = info["tree"]
        assert "checkpoints" in tree
        assert isinstance(tree["checkpoints"], list)

    def test_single_session_has_no_checkpoints(self, savant_session_dir):
        """A single session (no chain) has 0 checkpoints."""
        from app import savant_get_session_detail
        info = savant_get_session_detail(savant_session_dir)
        assert len(info["tree"]["checkpoints"]) == 0

    def test_tree_has_rewind_snapshots_list(self, savant_session_dir):
        from app import savant_get_session_detail
        info = savant_get_session_detail(savant_session_dir)
        tree = info["tree"]
        assert "rewind_snapshots" in tree
        assert isinstance(tree["rewind_snapshots"], list)


class TestSavantChainCheckpoints:
    """Verify chain sessions produce checkpoint entries."""

    @pytest.fixture
    def savant_chain_sessions(self, tmp_path, monkeypatch):
        """Create a chain of 3 sessions (root + 2 continuations)."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        root_id = "20260415_100000_root01"
        child1_id = "20260415_110000_child1"
        child2_id = "20260415_120000_child2"

        for sid, start, msgs in [
            (root_id, "2026-04-15T10:00:00Z", [
                {"role": "user", "content": "Start task"},
                {"role": "assistant", "content": "Working on it"}
            ]),
            (child1_id, "2026-04-15T11:00:00Z", [
                {"role": "user", "content": "Continue please"},
                {"role": "assistant", "content": "Continuing work"}
            ]),
            (child2_id, "2026-04-15T12:00:00Z", [
                {"role": "user", "content": "Finish up"},
                {"role": "assistant", "content": "All done"}
            ]),
        ]:
            data = {
                "model": "claude-opus-4-6",
                "platform": "cli",
                "session_start": start,
                "last_updated": start.replace("T", "T").replace(":00:00Z", ":30:00Z"),
                "messages": msgs,
            }
            (sessions_dir / f"session_{sid}.json").write_text(json.dumps(data))

        # Build fake chain info
        chain_map = {
            root_id: {
                "chain": [root_id, child1_id, child2_id],
                "tip_id": child2_id,
                "db_rows": {},
            },
            child1_id: {
                "chain": [root_id, child1_id, child2_id],
                "tip_id": child2_id,
                "db_rows": {},
            },
            child2_id: {
                "chain": [root_id, child1_id, child2_id],
                "tip_id": child2_id,
                "db_rows": {},
            },
        }

        monkeypatch.setattr("app.SAVANT_SESSIONS_DIR", str(sessions_dir))
        monkeypatch.setattr("app._savant_build_session_chains", lambda: chain_map)

        return root_id

    def test_chain_has_two_checkpoints(self, savant_chain_sessions):
        from app import savant_get_session_detail
        info = savant_get_session_detail(savant_chain_sessions)
        assert info["checkpoint_count"] == 2
        assert len(info["tree"]["checkpoints"]) == 2

    def test_checkpoint_entries_have_name_and_mtime(self, savant_chain_sessions):
        from app import savant_get_session_detail
        info = savant_get_session_detail(savant_chain_sessions)
        for cp in info["tree"]["checkpoints"]:
            assert "name" in cp
            assert "mtime" in cp
            assert "size" in cp
            assert "path" in cp

    def test_checkpoint_names_are_numbered(self, savant_chain_sessions):
        from app import savant_get_session_detail
        info = savant_get_session_detail(savant_chain_sessions)
        names = [cp["name"] for cp in info["tree"]["checkpoints"]]
        # Should be numbered like "Checkpoint 1", "Checkpoint 2"
        assert "1" in names[0]
        assert "2" in names[1]
